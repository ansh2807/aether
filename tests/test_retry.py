"""Tests for WO-03: retry with backoff before burning a failover.

Covers:
  * retries happen at the adapter layer (same provider, one ExecutionResult
    per top-level `execute` call)
  * only NETWORK/TIMEOUT are retried; AUTH (and friends) fail over immediately
  * `network_retries: 0` disables retrying
  * retries are exhausted correctly and the final failure is returned
  * CircuitBreaker.allow() enforces half_open_max_calls
"""

from __future__ import annotations

from typing import Optional

import pytest

from aether.adapters import retry as retry_mod
from aether.adapters.base import BaseAdapter
from aether.adapters.retry import backoff_delay, execute_with_retry, is_retryable
from aether.core.circuit import CircuitBreaker
from aether.config.settings import CircuitBreakerSettings
from aether.domain.enums import AdapterType, CircuitState, FailureClass
from aether.domain.models import ExecutionRequest, ExecutionResult, ProviderConfig


def _config(provider_id: str = "p1") -> ProviderConfig:
    return ProviderConfig(
        provider_id=provider_id,
        display_name=provider_id,
        adapter=AdapterType.MOCK,
        enabled=True,
        for_testing=True,
    )


def _request(provider_id: str = "p1") -> ExecutionRequest:
    return ExecutionRequest(task_id="t1", provider_id=provider_id, prompt="do the thing")


class _ScriptedAdapter(BaseAdapter):
    """Test double: returns a scripted sequence of outcomes, one per attempt.

    Each entry in `script` is either a FailureClass (attempt fails with that
    class) or None (attempt succeeds). Once the script is exhausted, the last
    entry repeats -- so a script of all-failures models a provider that never
    recovers.
    """

    def __init__(
        self,
        config: ProviderConfig,
        script: list[Optional[FailureClass]],
        network_retries: Optional[int] = None,
    ) -> None:
        super().__init__(config, network_retries=network_retries)
        self.script = list(script)
        self.attempts = 0

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return await self._execute_with_retry(lambda: self._execute_once(request))

    async def _execute_once(self, request: ExecutionRequest) -> ExecutionResult:
        idx = min(self.attempts, len(self.script) - 1)
        outcome = self.script[idx]
        self.attempts += 1
        if outcome is None:
            return ExecutionResult(
                execution_id=request.execution_id,
                provider_id=self.provider_id,
                success=True,
                output="ok",
            )
        return ExecutionResult(
            execution_id=request.execution_id,
            provider_id=self.provider_id,
            success=False,
            error=f"scripted failure: {outcome.value}",
            failure_class=outcome,
        )


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite fast: shrink the base delay tests exercise retries with."""
    monkeypatch.setattr(retry_mod, "BASE_DELAY_SECONDS", 0.001)
    monkeypatch.setattr(retry_mod, "MAX_DELAY_SECONDS", 0.01)


# --- acceptance criteria ----------------------------------------------------


async def test_two_network_failures_then_success_same_provider():
    adapter = _ScriptedAdapter(
        _config("p1"), [FailureClass.NETWORK, FailureClass.NETWORK, None], network_retries=3
    )
    result = await adapter.execute(_request("p1"))

    assert result.success is True
    assert result.provider_id == "p1"
    assert adapter.attempts == 3


async def test_auth_failure_returns_immediately_zero_retries():
    adapter = _ScriptedAdapter(_config("p1"), [FailureClass.AUTH], network_retries=5)
    result = await adapter.execute(_request("p1"))

    assert result.success is False
    assert result.failure_class == FailureClass.AUTH
    assert adapter.attempts == 1


@pytest.mark.parametrize(
    "failure_class",
    [
        FailureClass.AUTH,
        FailureClass.QUOTA,
        FailureClass.POLICY_DENIED,
        FailureClass.CONTEXT,
        FailureClass.CANCELLED,
    ],
)
async def test_permanent_failure_classes_are_never_retried(failure_class: FailureClass):
    adapter = _ScriptedAdapter(_config("p1"), [failure_class], network_retries=5)
    result = await adapter.execute(_request("p1"))

    assert result.success is False
    assert adapter.attempts == 1
    assert is_retryable(failure_class) is False


async def test_network_retries_zero_disables_retrying():
    adapter = _ScriptedAdapter(_config("p1"), [FailureClass.NETWORK, None], network_retries=0)
    result = await adapter.execute(_request("p1"))

    assert result.success is False
    assert result.failure_class == FailureClass.NETWORK
    assert adapter.attempts == 1


async def test_retries_exhausted_makes_exactly_retries_plus_one_attempts():
    adapter = _ScriptedAdapter(_config("p1"), [FailureClass.NETWORK], network_retries=2)
    result = await adapter.execute(_request("p1"))

    assert result.success is False
    assert result.failure_class == FailureClass.NETWORK
    assert adapter.attempts == 3


async def test_timeout_is_retried_like_network():
    adapter = _ScriptedAdapter(
        _config("p1"), [FailureClass.TIMEOUT, None], network_retries=2
    )
    result = await adapter.execute(_request("p1"))

    assert result.success is True
    assert adapter.attempts == 2


def test_half_open_honours_half_open_max_calls():
    settings = CircuitBreakerSettings(
        failure_threshold=1, recovery_timeout_seconds=0, half_open_max_calls=1
    )
    cb = CircuitBreaker(settings)
    cb.record_failure("p1")
    assert cb.snapshot("p1").state == CircuitState.OPEN

    # cooldown is 0 -> first allow() flips to HALF_OPEN and grants the one probe
    assert cb.allow("p1") is True
    assert cb.snapshot("p1").state == CircuitState.HALF_OPEN

    # a second concurrent probe is over the half_open_max_calls=1 budget
    assert cb.allow("p1") is False
    assert cb.snapshot("p1").state == CircuitState.HALF_OPEN


def test_half_open_max_calls_of_two_allows_two_probes():
    settings = CircuitBreakerSettings(
        failure_threshold=1, recovery_timeout_seconds=0, half_open_max_calls=2
    )
    cb = CircuitBreaker(settings)
    cb.record_failure("p1")

    assert cb.allow("p1") is True
    assert cb.allow("p1") is True
    assert cb.allow("p1") is False


def test_half_open_probe_budget_resets_on_success():
    settings = CircuitBreakerSettings(
        failure_threshold=1, recovery_timeout_seconds=0, half_open_max_calls=1
    )
    cb = CircuitBreaker(settings)
    cb.record_failure("p1")
    assert cb.allow("p1") is True
    cb.record_success("p1")
    assert cb.snapshot("p1").state == CircuitState.CLOSED
    # CLOSED never consults the half-open budget
    assert cb.allow("p1") is True
    assert cb.allow("p1") is True


def test_half_open_failure_reopens_and_resets_budget():
    settings = CircuitBreakerSettings(
        failure_threshold=1, recovery_timeout_seconds=0, half_open_max_calls=1
    )
    cb = CircuitBreaker(settings)
    cb.record_failure("p1")
    assert cb.allow("p1") is True  # consumes the single half-open probe
    cb.record_failure("p1")  # probe failed -> back to OPEN
    assert cb.snapshot("p1").state == CircuitState.OPEN
    # cooldown still 0 -> immediately eligible for a fresh probe
    assert cb.allow("p1") is True


# --- unit-level coverage of the retry helper itself -------------------------


async def test_execute_with_retry_disabled_runs_once_on_permanent_failure():
    calls = 0

    async def call() -> ExecutionResult:
        nonlocal calls
        calls += 1
        return ExecutionResult(
            execution_id="e1", provider_id="p1", success=False, failure_class=FailureClass.NETWORK
        )

    result = await execute_with_retry(call, retries=0, provider_id="p1")
    assert calls == 1
    assert result.success is False


async def test_execute_with_retry_negative_retries_treated_as_zero():
    calls = 0

    async def call() -> ExecutionResult:
        nonlocal calls
        calls += 1
        return ExecutionResult(
            execution_id="e1", provider_id="p1", success=False, failure_class=FailureClass.TIMEOUT
        )

    result = await execute_with_retry(call, retries=-3, provider_id="p1")
    assert calls == 1
    assert result.success is False


def test_backoff_delay_is_bounded_and_grows_with_attempt():
    for attempt in range(6):
        delay = backoff_delay(attempt)
        assert 0.0 <= delay <= retry_mod.MAX_DELAY_SECONDS
