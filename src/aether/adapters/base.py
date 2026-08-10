"""Base adapter helpers and handoff parsing."""

from __future__ import annotations

import re
from typing import Any, AsyncIterator

from aether.domain.enums import FailureClass, HandoffStatus, ProviderAvailability
from aether.domain.models import (
    AuthStatus,
    CapabilityProfile,
    ExecutionRequest,
    HandoffReport,
    HealthStatus,
    LimitSnapshot,
    ProviderConfig,
    StreamChunk,
    UsageSnapshot,
    utcnow,
)


class BaseAdapter:
    """Shared adapter utilities. Subclasses implement real I/O."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.provider_id = config.provider_id
        self._usage = UsageSnapshot(provider_id=config.provider_id)
        self._active: dict[str, Any] = {}
        self._initialized = False

    async def initialize(self) -> None:
        self._initialized = True

    async def authenticate(self) -> AuthStatus:
        return AuthStatus(authenticated=True, method="none", message="no auth required")

    async def health_check(self) -> HealthStatus:
        return HealthStatus(
            provider_id=self.provider_id,
            available=self.config.enabled,
            availability=ProviderAvailability.AVAILABLE
            if self.config.enabled
            else ProviderAvailability.DISABLED,
            message="base health",
            checked_at=utcnow(),
        )

    async def get_capabilities(self) -> CapabilityProfile:
        return CapabilityProfile(
            scores=dict(self.config.capabilities),
            roles_preferred=list(self.config.roles_preferred),
        )

    async def stream(self, request: ExecutionRequest) -> AsyncIterator[StreamChunk]:
        result = await self.execute(request)
        yield StreamChunk(execution_id=request.execution_id, text=result.output, done=True)

    async def cancel(self, execution_id: str) -> None:
        self._active.pop(execution_id, None)

    async def cancel_all(self) -> None:
        """Cancel every in-flight execution for this provider."""
        for execution_id in list(self._active.keys()):
            await self.cancel(execution_id)

    async def get_usage(self) -> UsageSnapshot:
        return self._usage

    async def get_limits(self) -> LimitSnapshot:
        limits = self.config.limits or {}
        return LimitSnapshot(
            requests_per_minute=limits.get("requests_per_minute"),
            daily_requests=limits.get("daily_requests"),
            remaining_daily=None,
            notes=self.config.status_note,
        )

    async def shutdown(self) -> None:
        self._active.clear()
        self._initialized = False

    def _record_request(self, tokens: int = 0) -> None:
        self._usage.requests += 1
        self._usage.daily_requests += 1
        self._usage.estimated_tokens += tokens
        self._usage.last_request_at = utcnow()

    def _record_failure(self) -> None:
        self._usage.failures += 1

    @staticmethod
    def classify_error(text: str) -> FailureClass:
        t = (text or "").lower()
        if "timeout" in t or "timed out" in t:
            return FailureClass.TIMEOUT
        if "401" in t or "403" in t or "auth" in t or "unauthor" in t:
            return FailureClass.AUTH
        if "429" in t or "rate limit" in t or "too many requests" in t:
            return FailureClass.RATE_LIMIT
        if "quota" in t or "billing" in t or "usage limit" in t:
            return FailureClass.QUOTA
        if "context" in t and ("length" in t or "window" in t or "overflow" in t or "too long" in t):
            return FailureClass.CONTEXT
        if "network" in t or "connection" in t or "dns" in t or "unreachable" in t:
            return FailureClass.NETWORK
        if "cancel" in t:
            return FailureClass.CANCELLED
        return FailureClass.UNKNOWN

    @classmethod
    def parse_handoff(
        cls,
        task_id: str,
        from_agent: str,
        output: str,
        requested: str = "",
    ) -> HandoffReport:
        """Extract structured handoff from free-form agent output when possible."""
        text = output or ""
        status = HandoffStatus.PARTIAL
        status_m = re.search(r"(?im)^\s*STATUS\s*:\s*(\w+)", text)
        if status_m:
            raw = status_m.group(1).upper()
            try:
                status = HandoffStatus(raw)
            except ValueError:
                status = HandoffStatus.PARTIAL

        def section_list(name: str) -> list[str]:
            # Match NAME: then bullets or comma-ish lines until next ALLCAPS header
            pat = rf"(?is){name}\s*:\s*(.*?)(?=\n\s*[A-Z][A-Z_ ]{{2,}}\s*:|\Z)"
            m = re.search(pat, text)
            if not m:
                return []
            body = m.group(1).strip()
            items: list[str] = []
            for line in body.splitlines():
                line = line.strip().lstrip("-*•").strip()
                if line:
                    items.append(line)
            if not items and body:
                items = [body[:500]]
            return items

        def section_text(name: str) -> str:
            items = section_list(name)
            return " ".join(items) if items else ""

        completed = section_list("COMPLETED")
        remaining = section_list("REMAINING")
        files_changed = section_list("FILES") or section_list("FILES_CHANGED")
        decisions = section_list("DECISIONS")
        tests = section_list("TESTS") or section_list("TESTS_EXECUTED")
        bugs = section_list("KNOWN_BUGS") or section_list("BUGS")
        assumptions = section_list("ASSUMPTIONS")
        next_action = section_text("RECOMMENDED_NEXT_ACTION") or section_text("NEXT")
        test_results = section_text("TEST_RESULTS") or section_text("TEST_RESULTS")

        if not completed and not remaining and text.strip():
            # unparseable — still create partial
            return HandoffReport(
                task_id=task_id,
                from_agent=from_agent,
                status=HandoffStatus.PARTIAL,
                requested=requested,
                completed=["raw execution finished with unparseable structured handoff"],
                remaining=[requested or "continue task"],
                known_bugs=["malformed_handoff"],
                recommended_next_action=requested or "Inspect raw output and continue",
                assumptions=["Handoff was synthesized by orchestrator"],
            )

        if status == HandoffStatus.COMPLETE and not remaining:
            remaining = []

        return HandoffReport(
            task_id=task_id,
            from_agent=from_agent,
            status=status,
            requested=requested,
            completed=completed,
            remaining=remaining,
            files_changed=files_changed,
            decisions=decisions,
            tests_executed=tests,
            test_results=test_results,
            known_bugs=bugs,
            assumptions=assumptions,
            recommended_next_action=next_action or (remaining[0] if remaining else "Review progress"),
        )
