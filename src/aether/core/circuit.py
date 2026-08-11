"""Per-provider circuit breaker."""

from __future__ import annotations

from datetime import timedelta

from aether.config.settings import CircuitBreakerSettings
from aether.domain.enums import CircuitState
from aether.domain.models import CircuitSnapshot, utcnow


class CircuitBreaker:
    def __init__(self, settings: CircuitBreakerSettings) -> None:
        self.settings = settings
        self._states: dict[str, CircuitSnapshot] = {}
        # Probes granted in the current HALF_OPEN window, per provider. Not
        # part of CircuitSnapshot (a persisted/serializable model) — this is
        # transient bookkeeping reset whenever the circuit leaves HALF_OPEN.
        self._half_open_calls: dict[str, int] = {}

    def _get(self, provider_id: str) -> CircuitSnapshot:
        if provider_id not in self._states:
            self._states[provider_id] = CircuitSnapshot(provider_id=provider_id)
        return self._states[provider_id]

    def _allow_half_open_probe(self, provider_id: str) -> bool:
        """Enforce settings.half_open_max_calls: only that many probes may
        pass through per HALF_OPEN window. Without this, HALF_OPEN let
        unlimited concurrent probes through, defeating the point of probing
        cautiously before fully reopening the circuit."""
        used = self._half_open_calls.get(provider_id, 0)
        limit = max(0, self.settings.half_open_max_calls)
        if used >= limit:
            return False
        self._half_open_calls[provider_id] = used + 1
        return True

    def allow(self, provider_id: str) -> bool:
        snap = self._get(provider_id)
        now = utcnow()
        if snap.state == CircuitState.CLOSED:
            return True
        if snap.state == CircuitState.OPEN:
            if snap.opened_at and now >= snap.opened_at + timedelta(
                seconds=self.settings.recovery_timeout_seconds
            ):
                snap.state = CircuitState.HALF_OPEN
                self._half_open_calls[provider_id] = 0
                return self._allow_half_open_probe(provider_id)
            return False
        # HALF_OPEN — allow only up to half_open_max_calls probes
        return self._allow_half_open_probe(provider_id)

    def record_success(self, provider_id: str) -> CircuitSnapshot:
        snap = self._get(provider_id)
        snap.failure_count = 0
        snap.state = CircuitState.CLOSED
        snap.last_success_at = utcnow()
        snap.opened_at = None
        self._half_open_calls.pop(provider_id, None)
        return snap

    def record_failure(self, provider_id: str) -> CircuitSnapshot:
        snap = self._get(provider_id)
        snap.failure_count += 1
        snap.last_failure_at = utcnow()
        if snap.state == CircuitState.HALF_OPEN:
            snap.state = CircuitState.OPEN
            snap.opened_at = utcnow()
            self._half_open_calls.pop(provider_id, None)
        elif snap.failure_count >= self.settings.failure_threshold:
            snap.state = CircuitState.OPEN
            snap.opened_at = utcnow()
        return snap

    def snapshot(self, provider_id: str) -> CircuitSnapshot:
        return self._get(provider_id)

    def all_snapshots(self) -> list[CircuitSnapshot]:
        return list(self._states.values())
