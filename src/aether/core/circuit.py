"""Per-provider circuit breaker.

Beyond the usual CLOSED/OPEN/HALF_OPEN, this breaker distinguishes failures
that time heals from failures that it does not.

OPEN is a timer: wait `recovery_timeout_seconds`, half-open, probe, and either
close or reopen. That is the right shape for a timeout, a crash, a network
blip, a rate limit — all of which are transient by nature.

It is the wrong shape for an expired or revoked API key. A 401 is still a 401
sixty seconds later, so the breaker cycles forever, and each half-open probe
spends a real subtask attempt on a request that cannot succeed. `DISABLED`
exists for that case: it never reopens on a clock, and it carries a reason the
operator can act on.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from aether.config.settings import CircuitBreakerSettings
from aether.domain.enums import CircuitState, FailureClass
from aether.domain.models import CircuitSnapshot, utcnow


class CircuitBreaker:
    # Failures that waiting cannot fix. Keep this set small and defensible:
    # marking something terminal that is merely slow to recover takes a healthy
    # provider out of rotation until a human notices, which is its own outage.
    #
    #   AUTH           a bad, expired or revoked credential. Needs a human.
    #   POLICY_DENIED  the provider refused on policy grounds. Retrying is
    #                  either pointless or abusive.
    #
    # Deliberately NOT terminal, though it may look tempting:
    #   RATE_LIMIT, QUOTA  recover on a clock — that is exactly what OPEN is for
    #   TIMEOUT, NETWORK, CRASH   transient by definition
    #   CONTEXT            our prompt was too big; a different subtask may fit
    #   VERIFICATION, MALFORMED_HANDOFF, INVALID_RESPONSE
    #                      the provider answered; the answer was poor. That is a
    #                      quality signal for routing, not a broken connection.
    TERMINAL_FAILURES: frozenset[FailureClass] = frozenset(
        {FailureClass.AUTH, FailureClass.POLICY_DENIED}
    )

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
        if snap.state == CircuitState.DISABLED:
            # No timer, deliberately. Cleared only via reset().
            return False
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

    def record_failure(
        self,
        provider_id: str,
        failure_class: Optional[FailureClass] = None,
    ) -> CircuitSnapshot:
        """Record a failure, taking it out of rotation appropriately.

        `failure_class` is optional so existing callers keep working, but
        passing it is what lets a terminal failure be treated as terminal.
        Without it every failure is assumed transient — the old behaviour.
        """
        snap = self._get(provider_id)
        snap.failure_count += 1
        snap.last_failure_at = utcnow()

        if failure_class in self.TERMINAL_FAILURES:
            # One strike. Waiting will not produce a different credential, and
            # the failure threshold would otherwise burn N subtask attempts
            # discovering that.
            snap.state = CircuitState.DISABLED
            snap.opened_at = utcnow()
            snap.disabled_reason = (
                f"{failure_class.value} — needs operator action, not a retry. "
                f"Fix the credential (aether connect {provider_id}); takes effect "
                f"on the next run."
            )
            self._half_open_calls.pop(provider_id, None)
            return snap

        if snap.state == CircuitState.HALF_OPEN:
            snap.state = CircuitState.OPEN
            snap.opened_at = utcnow()
            self._half_open_calls.pop(provider_id, None)
        elif snap.failure_count >= self.settings.failure_threshold:
            snap.state = CircuitState.OPEN
            snap.opened_at = utcnow()
        return snap

    def reset(self, provider_id: str) -> CircuitSnapshot:
        """Explicitly return a provider to rotation.

        The only way out of DISABLED within a process. Deliberately not
        time-based: the whole point is that nothing about the situation changes
        on its own.

        Scope note: breaker state lives in memory and is never persisted, so
        DISABLED lasts for the life of the process and a restart clears it.
        That is the intended behaviour — a restart is a reasonable moment to
        assume the operator fixed the credential — but it means an out-of-process
        `reset` command would act on its own empty breaker and do nothing. Call
        this from inside the process that owns the state (the API server, or a
        long-running task run).
        """
        snap = self._get(provider_id)
        snap.state = CircuitState.CLOSED
        snap.failure_count = 0
        snap.opened_at = None
        snap.disabled_reason = None
        self._half_open_calls.pop(provider_id, None)
        return snap

    def snapshot(self, provider_id: str) -> CircuitSnapshot:
        return self._get(provider_id)

    def all_snapshots(self) -> list[CircuitSnapshot]:
        return list(self._states.values())
