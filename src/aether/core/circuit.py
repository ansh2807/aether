"""Per-provider circuit breaker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aether.config.settings import CircuitBreakerSettings
from aether.domain.enums import CircuitState
from aether.domain.models import CircuitSnapshot, utcnow


class CircuitBreaker:
    def __init__(self, settings: CircuitBreakerSettings) -> None:
        self.settings = settings
        self._states: dict[str, CircuitSnapshot] = {}

    def _get(self, provider_id: str) -> CircuitSnapshot:
        if provider_id not in self._states:
            self._states[provider_id] = CircuitSnapshot(provider_id=provider_id)
        return self._states[provider_id]

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
                return True
            return False
        # HALF_OPEN — allow limited probes
        return True

    def record_success(self, provider_id: str) -> CircuitSnapshot:
        snap = self._get(provider_id)
        snap.failure_count = 0
        snap.state = CircuitState.CLOSED
        snap.last_success_at = utcnow()
        snap.opened_at = None
        return snap

    def record_failure(self, provider_id: str) -> CircuitSnapshot:
        snap = self._get(provider_id)
        snap.failure_count += 1
        snap.last_failure_at = utcnow()
        if snap.state == CircuitState.HALF_OPEN:
            snap.state = CircuitState.OPEN
            snap.opened_at = utcnow()
        elif snap.failure_count >= self.settings.failure_threshold:
            snap.state = CircuitState.OPEN
            snap.opened_at = utcnow()
        return snap

    def snapshot(self, provider_id: str) -> CircuitSnapshot:
        return self._get(provider_id)

    def all_snapshots(self) -> list[CircuitSnapshot]:
        return list(self._states.values())
