"""Agent selection / routing engine."""

from __future__ import annotations

import time
from typing import Optional

from aether.adapters.registry import AgentRegistry
from aether.config.settings import AetherSettings
from aether.core.circuit import CircuitBreaker
from aether.core.modality import MEDIA_CAPABILITIES, describe_gap
from aether.core.quota import QuotaManager
from aether.domain.enums import AgentRole, ProviderAvailability
from aether.domain.models import HealthStatus, RouteDecision, Subtask, Task
from aether.interfaces.adapters import AgentAdapter
from aether.observability.logging import get_logger

log = get_logger("aether.router")

# health_check() can be a live HTTP request for API providers. Routing calls it
# once per candidate per subtask, so without a cache a 5-subtask task probes
# every provider 5 times. Cache each result for this long before re-probing.
HEALTH_CACHE_TTL_SECONDS = 30.0

# In-flight count at which the load penalty is fully applied. Four matches the
# default max_concurrent_subtasks: by the time a provider holds the whole
# concurrency budget it should be maximally unattractive, and no more so.
LOAD_SATURATION = 4

ROLE_CAPABILITY = {
    AgentRole.ARCHITECT: "architecture",
    AgentRole.IMPLEMENTER: "coding",
    AgentRole.REVIEWER: "review",
    AgentRole.SECURITY_REVIEWER: "review",
    AgentRole.TESTER: "testing",
    AgentRole.DEBUGGER: "coding",
    AgentRole.RESEARCHER: "research",
    AgentRole.FINAL_REVIEWER: "review",
    AgentRole.GENERAL: "reasoning",
}


class AgentRouter:
    def __init__(
        self,
        settings: AetherSettings,
        registry: AgentRegistry,
        quota: QuotaManager,
        circuit: CircuitBreaker,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.quota = quota
        self.circuit = circuit
        self._health_cache: dict[str, tuple[float, HealthStatus]] = {}
        # provider_id -> subtasks currently executing on it
        self._inflight: dict[str, int] = {}
        # Why the last select() returned None, when the reason is
        # permanent (a modality nothing can serve) rather than transient.
        self.last_failure_reason: str = ""

    # --- concurrent load ------------------------------------------------
    #
    # Selection used to be stateless with respect to work already running, so
    # N subtasks dispatched together scored identically and all chose the same
    # provider — queueing behind one rate limit while healthy providers idled.
    # Parallel mode is precisely where that hurts.
    #
    # The count is incremented at dispatch and decremented on completion, so it
    # is always current by the time the next selection reads it. That is what
    # makes a plain deterministic penalty sufficient here: the classic
    # thundering-herd problem needs *stale* load information, and there is none
    # to be stale. (The roadmap sketched power-of-two-choices for this; a
    # random tie-break would buy nothing over fresh counts and would cost the
    # reproducibility that makes routing decisions explicable.)

    def note_dispatch(self, provider_id: str) -> None:
        self._inflight[provider_id] = self._inflight.get(provider_id, 0) + 1

    def note_complete(self, provider_id: str) -> None:
        remaining = self._inflight.get(provider_id, 0) - 1
        if remaining > 0:
            self._inflight[provider_id] = remaining
        else:
            self._inflight.pop(provider_id, None)

    def inflight(self, provider_id: str) -> int:
        return self._inflight.get(provider_id, 0)

    def invalidate_health(self, provider_id: str) -> None:
        """Drop a provider's cached health result — call this when it fails."""
        self._health_cache.pop(provider_id, None)

    async def _cached_health(self, provider_id: str, adapter: AgentAdapter) -> HealthStatus:
        """health_check() with a short TTL cache (see HEALTH_CACHE_TTL_SECONDS)."""
        cached = self._health_cache.get(provider_id)
        now = time.monotonic()
        if cached is not None and (now - cached[0]) < HEALTH_CACHE_TTL_SECONDS:
            return cached[1]
        health = await adapter.health_check()
        self._health_cache[provider_id] = (now, health)
        return health

    def _has_real_provider(self) -> bool:
        """True when at least one non-testing provider is enabled and registered."""
        for pid in self.registry.list_ids():
            cfg = self.registry.get_config(pid)
            if cfg is not None and cfg.enabled and not cfg.for_testing:
                return True
        return False

    async def select(
        self,
        task: Task,
        subtask: Optional[Subtask] = None,
        role: Optional[AgentRole] = None,
        exclude: Optional[list[str]] = None,
        required_capabilities: Optional[list[str]] = None,
    ) -> Optional[RouteDecision]:
        exclude_set = set(exclude or [])
        role = role or (subtask.role if subtask else AgentRole.GENERAL)
        caps = list(required_capabilities or (subtask.required_capabilities if subtask else ["reasoning"]))
        primary_cap = ROLE_CAPABILITY.get(role, caps[0] if caps else "reasoning")

        has_real_provider = self._has_real_provider()

        # Modality is a gate, not a weight. A subtask that must produce an
        # image or a video has to go to a provider that can actually produce
        # one; scoring it softly would let a text model win on capability and
        # hand back a paragraph describing the artifact. See core/modality.py.
        required_media = MEDIA_CAPABILITIES & set(caps)

        candidates: list[tuple[str, float, list[str]]] = []
        for pid in self.registry.list_ids():
            if pid in exclude_set:
                continue
            cfg = self.registry.get_config(pid)
            if cfg is None:
                continue
            if not cfg.enabled:
                continue
            if (
                cfg.for_testing
                and has_real_provider
                and not self.settings.routing.allow_testing_fallback
            ):
                # never let a fake agent absorb work a real provider failed at
                continue
            if not self.circuit.allow(pid):
                continue
            avail = self.quota.availability(pid)
            if avail in (
                ProviderAvailability.RATE_LIMITED,
                ProviderAvailability.QUOTA_EXHAUSTED,
                ProviderAvailability.CIRCUIT_OPEN,
            ):
                continue

            adapter = self.registry.get(pid)
            if adapter is None:
                continue
            health = await self._cached_health(pid, adapter)
            if not health.available and not cfg.for_testing:
                continue

            profile = await adapter.get_capabilities()

            # The gate. A provider that does not declare every required
            # modality is not a candidate at any score, and the mock is not
            # exempt — fabricating an image is the same lie as fabricating a
            # success.
            if required_media and not all(profile.score(m, 0) > 0 for m in required_media):
                continue

            cap_score = profile.score(primary_cap, 0)
            for c in caps:
                cap_score = max(cap_score, profile.score(c, 0))
            if cap_score <= 0 and not cfg.for_testing:
                continue

            reasons: list[str] = []
            w = self.settings.routing.weights
            score = 0.0
            score += w.get("capability", 0.45) * (cap_score / 10.0)
            reasons.append(f"capability:{primary_cap}={cap_score}")

            avail_score = 1.0 if health.available else 0.2
            if avail == ProviderAvailability.AVAILABLE:
                avail_score = 1.0
            score += w.get("availability", 0.25) * avail_score
            reasons.append(f"availability={avail.value}")

            health_score = 1.0 if health.available else 0.0
            if health.latency_ms is not None:
                health_score *= max(0.1, 1.0 - min(health.latency_ms, 5000) / 5000)
            score += w.get("health", 0.15) * health_score

            # prefer preferred roles
            if role.value in (cfg.roles_preferred or []):
                score += 0.08
                reasons.append(f"preferred_role={role.value}")

            # user preferences
            for pref in self.settings.routing.user_preferences:
                if pref == f"prefer:{pid}":
                    score += 0.1
                    reasons.append("user_prefer")
                if pref == f"avoid:{pid}":
                    score -= 0.5
                    reasons.append("user_avoid")

            # failure history
            usage = self.quota.get_usage(pid)
            fail_penalty = min(usage.failures, 10) / 10.0
            score -= w.get("failure_history", 0.05) * fail_penalty

            # concurrent load — spread parallel subtasks instead of stacking
            # them on whichever provider scores best in the abstract. Saturates
            # at LOAD_SATURATION so a busy provider is penalised, not banished:
            # with one strong provider and one weak one, the strong one should
            # still take the second subtask.
            busy = self.inflight(pid)
            if busy:
                score -= w.get("load", 0.10) * min(busy, LOAD_SATURATION) / LOAD_SATURATION
                reasons.append(f"inflight={busy}")

            # slight preference to stick with active agent for continuity (not on failover)
            if task.active_agent == pid and not exclude_set:
                score += 0.05
                reasons.append("continuity")

            candidates.append((pid, score, reasons))

        if not candidates:
            # Distinguish "everyone is busy" from "nobody can do this at all".
            # The second is a permanent condition and needs a different answer
            # from the operator, so it must not look like the first.
            if required_media:
                gap = describe_gap(required_media) or (
                    f"no enabled provider declares {', '.join(sorted(required_media))}"
                )
                log.warning(
                    "no_route_for_modality",
                    task_id=task.task_id,
                    role=role.value,
                    modalities=sorted(required_media),
                )
                self.last_failure_reason = gap
            else:
                log.warning("no_route_candidates", task_id=task.task_id, role=role.value)
                self.last_failure_reason = ""
            return None

        candidates.sort(key=lambda x: x[1], reverse=True)
        best_id, best_score, reasons = candidates[0]
        alts = [c[0] for c in candidates[1:5]]
        decision = RouteDecision(
            provider_id=best_id,
            score=best_score,
            reasons=reasons,
            role=role,
            alternatives=alts,
        )
        log.info("route_selected", task_id=task.task_id, provider=best_id, score=best_score, role=role.value)
        return decision

    async def select_failover(
        self,
        task: Task,
        failed_provider: str,
        subtask: Optional[Subtask] = None,
        role: Optional[AgentRole] = None,
    ) -> Optional[RouteDecision]:
        # the provider just failed — its cached health is now stale
        self.invalidate_health(failed_provider)
        exclude = [failed_provider]
        if self.settings.routing.prefer_different_agent_on_failover:
            exclude.extend(task.participating_agents[-1:] if task.participating_agents else [])
        return await self.select(task, subtask=subtask, role=role, exclude=list(set(exclude)))
