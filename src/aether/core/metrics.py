"""Runtime metrics and task traces."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

from aether.core.state_store import JsonStateStore
from aether.domain.models import utcnow

MAX_TRACE_EVENTS = 500
MAX_TRACED_TASKS = 200

_COUNTERS = (
    "task_started",
    "task_completed",
    "task_failed",
    "task_cancelled",
    "handoffs",
    "failovers",
    "verifications_passed",
    "verifications_failed",
)


class MetricsCollector:
    """Runtime metrics.

    Persisted to a JSON state file when one is supplied, so `aether metrics`
    and `aether task trace` report on runs from the server process rather than
    the empty counters of the CLI process that just started.
    """

    def __init__(self, store: Optional[JsonStateStore] = None) -> None:
        self.store = store
        self.task_started = 0
        self.task_completed = 0
        self.task_failed = 0
        self.task_cancelled = 0
        self.handoffs = 0
        self.failovers = 0
        self.verifications_passed = 0
        self.verifications_failed = 0
        self.agent_latency_ms: dict[str, list[float]] = defaultdict(list)
        self.agent_success: dict[str, int] = defaultdict(int)
        self.agent_failure: dict[str, int] = defaultdict(int)
        self.task_traces: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._reload()

    # --- persistence -----------------------------------------------------

    def _transaction(self):
        """Exclusive lock across reload+mutate+flush; no-op without a store."""
        if self.store is None:
            from contextlib import nullcontext

            return nullcontext()
        return self.store.transaction()

    def _reload(self) -> None:
        if self.store is None:
            return
        data = self.store.load()
        if not data:
            return
        for name in _COUNTERS:
            setattr(self, name, int(data.get(name, 0) or 0))
        self.agent_latency_ms = defaultdict(list, {k: list(v) for k, v in (data.get("agent_latency_ms") or {}).items()})
        self.agent_success = defaultdict(int, {k: int(v) for k, v in (data.get("agent_success") or {}).items()})
        self.agent_failure = defaultdict(int, {k: int(v) for k, v in (data.get("agent_failure") or {}).items()})
        self.task_traces = defaultdict(
            list, {k: list(v) for k, v in (data.get("task_traces") or {}).items()}
        )

    def _flush(self) -> None:
        if self.store is None:
            return
        traces = dict(list(self.task_traces.items())[-MAX_TRACED_TASKS:])
        self.store.save(
            {
                **{name: getattr(self, name) for name in _COUNTERS},
                "agent_latency_ms": {k: v[-MAX_TRACE_EVENTS:] for k, v in self.agent_latency_ms.items()},
                "agent_success": dict(self.agent_success),
                "agent_failure": dict(self.agent_failure),
                "task_traces": traces,
            }
        )

    def record_task_event(self, task_id: str, event: str, **fields: Any) -> None:
        with self._transaction():
            self._reload()
            self._append_event(task_id, event, **fields)
            self._flush()

    def _append_event(self, task_id: str, event: str, **fields: Any) -> None:
        """Mutate in place. Callers own the surrounding reload/flush pair so a
        reload never discards a change made just before it."""
        trace = self.task_traces[task_id]
        trace.append({"event": event, "ts": utcnow().isoformat(), **fields})
        if len(trace) > MAX_TRACE_EVENTS:
            del trace[:-MAX_TRACE_EVENTS]
        if event == "started":
            self.task_started += 1
        elif event == "completed":
            self.task_completed += 1
        elif event == "failed":
            self.task_failed += 1
        elif event == "cancelled":
            self.task_cancelled += 1
        elif event == "handoff":
            self.handoffs += 1
        elif event == "failover":
            self.failovers += 1
        elif event == "verification_passed":
            self.verifications_passed += 1
        elif event == "verification_failed":
            self.verifications_failed += 1

    def record_agent_execution(
        self,
        provider_id: str,
        latency_ms: float,
        success: bool,
        task_id: Optional[str] = None,
    ) -> None:
        self._reload()
        self.agent_latency_ms[provider_id].append(latency_ms)
        if success:
            self.agent_success[provider_id] += 1
        else:
            self.agent_failure[provider_id] += 1
        if task_id:
            self._append_event(
                task_id,
                "agent_execution",
                provider=provider_id,
                latency_ms=latency_ms,
                success=success,
            )
        self._flush()

    def agent_stats(self, provider_id: str) -> dict[str, Any]:
        lat = self.agent_latency_ms.get(provider_id) or []
        ok = self.agent_success.get(provider_id, 0)
        fail = self.agent_failure.get(provider_id, 0)
        total = ok + fail
        return {
            "provider_id": provider_id,
            "success": ok,
            "failure": fail,
            "success_rate": (ok / total) if total else None,
            "avg_latency_ms": (sum(lat) / len(lat)) if lat else None,
            "p95_latency_ms": sorted(lat)[int(len(lat) * 0.95) - 1] if len(lat) >= 2 else (lat[0] if lat else None),
            "samples": len(lat),
        }

    def task_trace(self, task_id: str) -> list[dict[str, Any]]:
        self._reload()
        return list(self.task_traces.get(task_id, []))

    def summary(self) -> dict[str, Any]:
        self._reload()
        providers = set(self.agent_latency_ms) | set(self.agent_success) | set(self.agent_failure)
        return {
            "tasks": {
                "started": self.task_started,
                "completed": self.task_completed,
                "failed": self.task_failed,
                "cancelled": self.task_cancelled,
                "success_rate": (
                    self.task_completed / self.task_started if self.task_started else None
                ),
            },
            "handoffs": self.handoffs,
            "failovers": self.failovers,
            "verifications": {
                "passed": self.verifications_passed,
                "failed": self.verifications_failed,
            },
            "agents": [self.agent_stats(p) for p in sorted(providers)],
        }
