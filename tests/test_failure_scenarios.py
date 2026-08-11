"""Explicit coverage for the 18 failure scenarios (where automatable)."""

from __future__ import annotations

import asyncio

import pytest

from aether.adapters.base import BaseAdapter
from aether.adapters.mock_adapter import MockAdapter
from aether.adapters.unavailable import UnavailableAdapter
from aether.app import AetherApp
from aether.core.circuit import CircuitBreaker
from aether.domain.enums import (
    AdapterType,
    FailureClass,
    HandoffStatus,
    TaskStatus,
    VerificationStatus,
)
from aether.domain.models import ExecutionRequest, ProviderConfig
from aether.config.settings import CircuitBreakerSettings


def _count(events, action: str) -> int:
    """How many times a mechanism actually fired on the timeline."""
    return sum(1 for e in events if e.action == action)


async def wait_terminal(app: AetherApp, task_id: str, timeout: float = 35.0):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        t = await app.tasks.get(task_id)
        if t and t.status in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.PAUSED,
        }:
            return t
        await asyncio.sleep(0.1)
    t = await app.tasks.get(task_id)
    raise TimeoutError(str(t.status if t else None))


@pytest.mark.asyncio
async def test_1_agent_timeout(app: AetherApp):
    """A timeout must be classified and routed around, not merely survived."""
    app.registry.get("mock").set_fail_next(FailureClass.TIMEOUT)
    task = await app.orchestrator.submit("Build app with timeout simulation")
    done = await wait_terminal(app, task.task_id)

    events = await app.repo.list_events(task.task_id)
    errors = [e for e in events if e.action == "error"]
    assert errors, "the timeout was never recorded"
    assert any("TIMEOUT" in e.detail for e in errors), [e.detail for e in errors]
    assert _count(events, "failover") >= 1, "no failover was attempted"
    assert app.metrics.failovers >= 1
    assert done.status == TaskStatus.COMPLETED, "another agent should have finished it"


@pytest.mark.asyncio
async def test_2_agent_crash(app: AetherApp):
    """A crash checkpoints and hands off rather than losing the subtask."""
    app.registry.get("mock").set_fail_next(FailureClass.CRASH)
    task = await app.orchestrator.submit("Build app with crash simulation")
    done = await wait_terminal(app, task.task_id)

    events = await app.repo.list_events(task.task_id)
    assert _count(events, "failover") >= 1
    assert await app.repo.list_checkpoints(task.task_id), "no checkpoint around the crash"
    assert await app.repo.list_handoffs(task.task_id), "no handoff was synthesised"
    assert done.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_3_cli_unavailable():
    cfg = ProviderConfig(
        provider_id="missing_cli",
        display_name="Missing",
        adapter=AdapterType.CLI,
        enabled=True,
        command="this-cli-does-not-exist-aether-xyz",
    )
    from aether.adapters.cli_adapter import CliAgentAdapter

    adapter = CliAgentAdapter(cfg)
    health = await adapter.health_check()
    assert not health.available
    result = await adapter.execute(
        ExecutionRequest(task_id="T", provider_id="missing_cli", prompt="hi")
    )
    assert not result.success
    assert result.failure_class == FailureClass.CRASH


@pytest.mark.asyncio
async def test_4_auth_failure(app: AetherApp):
    """Auth is permanent for the attempt: penalise the provider and move on."""
    app.registry.get("mock").set_fail_next(FailureClass.AUTH)
    task = await app.orchestrator.submit("Build with auth fail")
    done = await wait_terminal(app, task.task_id)

    events = await app.repo.list_events(task.task_id)
    assert any("AUTH" in e.detail for e in events if e.action == "error")
    # not failure_count: a later success on the same provider resets it to 0,
    # which is correct breaker behaviour. last_failure_at is the durable trace.
    assert app.circuit.snapshot("mock").last_failure_at is not None, (
        "circuit never saw the failure"
    )
    assert app.quota.get_usage("mock").failures >= 1, "quota did not record it"
    assert done.status == TaskStatus.COMPLETED
    assert "mock_b" in done.participating_agents, "never actually failed over"


@pytest.mark.asyncio
async def test_5_rate_limit(app: AetherApp):
    """A 429 must put the provider on cooldown, not just fail the call."""
    from aether.domain.enums import ProviderAvailability

    app.registry.get("mock").set_fail_next(FailureClass.RATE_LIMIT)
    task = await app.orchestrator.submit("Build with rate limit")
    done = await wait_terminal(app, task.task_id)

    assert app.quota.availability("mock") == ProviderAvailability.RATE_LIMITED, (
        "rate limit set no cooldown, so the provider would be retried immediately"
    )
    assert done.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_6_quota_exhaustion(app: AetherApp):
    """Quota exhaustion must take the provider out of the pool."""
    from aether.domain.enums import ProviderAvailability

    app.registry.get("mock").set_fail_next(FailureClass.QUOTA)
    task = await app.orchestrator.submit("Build with quota exhaust")
    done = await wait_terminal(app, task.task_id)

    assert app.quota.availability("mock") == ProviderAvailability.QUOTA_EXHAUSTED, (
        "quota failure did not mark the provider exhausted"
    )
    assert done.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_7_network_failure(app: AetherApp):
    """Network failures are classified and failed over."""
    app.registry.get("mock").set_fail_next(FailureClass.NETWORK)
    task = await app.orchestrator.submit("Build with network fail")
    done = await wait_terminal(app, task.task_id)

    events = await app.repo.list_events(task.task_id)
    assert any("NETWORK" in e.detail for e in events if e.action == "error")
    assert _count(events, "failover") >= 1
    assert done.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_8_context_overflow(app: AetherApp):
    """Context overflow is its own class and is routed around."""
    app.registry.get("mock").set_fail_next(FailureClass.CONTEXT)
    task = await app.orchestrator.submit("Build with context overflow")
    done = await wait_terminal(app, task.task_id)

    events = await app.repo.list_events(task.task_id)
    assert any("CONTEXT" in e.detail for e in events if e.action == "error")
    assert _count(events, "failover") >= 1
    assert done.status == TaskStatus.COMPLETED


def test_9_invalid_response_classification():
    assert BaseAdapter.classify_error("invalid json response") in {
        FailureClass.UNKNOWN,
        FailureClass.INVALID_RESPONSE,
    }
    # direct enum
    assert FailureClass.INVALID_RESPONSE.value == "INVALID_RESPONSE"


def test_10_malformed_handoff_synthesis():
    ho = BaseAdapter.parse_handoff("T", "a", "no structure here")
    assert "malformed_handoff" in ho.known_bugs
    assert ho.status == HandoffStatus.PARTIAL


@pytest.mark.asyncio
async def test_11_12_incorrect_code_and_tests(app: AetherApp):
    """Broken code must fail verification and spawn a debugger subtask.

    The prompt says "broken" deliberately: that word is the trigger the mock
    adapter keys on to emit code that does not compile.
    """
    for provider in ("mock", "mock_b"):
        app.registry.get(provider).behavior["produce_bad_code"] = True

    task = await app.orchestrator.submit("Build broken code that fails verification")
    done = await wait_terminal(app, task.task_id, timeout=45)

    results = await app.repo.list_results(task.task_id)
    assert results, "verification never ran"
    assert any(r.status == VerificationStatus.FAILED for r in results), (
        f"broken code passed verification: {[r.status.value for r in results]}"
    )
    assert any("Fix verification failures" in s.title for s in done.subtasks), (
        "no debugger subtask was created for the failure"
    )
    assert app.metrics.verifications_failed >= 1


@pytest.mark.asyncio
async def test_14_orchestrator_restart_recovery(app: AetherApp, settings):
    task = await app.orchestrator.submit("Quick research note")
    done = await wait_terminal(app, task.task_id)
    tid = done.task_id
    # simulate mid-flight task
    mid = await app.tasks.get(tid)
    assert mid
    mid.status = TaskStatus.RUNNING
    await app.tasks.save(mid)
    await app.stop()

    app2 = AetherApp(settings)
    await app2.start()
    try:
        # recover_running is called in start — may requeue
        loaded = await app2.tasks.get(tid)
        assert loaded is not None
        # either re-running or already completed from recover race
        if loaded.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            done2 = await wait_terminal(app2, tid, timeout=40)
            assert done2.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    finally:
        await app2.stop()


@pytest.mark.asyncio
async def test_16_provider_back_online_circuit():
    cb = CircuitBreaker(CircuitBreakerSettings(failure_threshold=1, recovery_timeout_seconds=0))
    cb.record_failure("p")
    assert not cb.allow("p") or cb.snapshot("p").state.value in {"OPEN", "HALF_OPEN"}
    # recovery_timeout_seconds=0, so the allow() call above already forced
    # OPEN -> HALF_OPEN and consumed the sole probe budget
    # (half_open_max_calls defaults to 1, now enforced by CircuitBreaker.allow
    # -- see WO-03). A second concurrent probe request is correctly refused
    # rather than let through unbounded.
    assert cb.snapshot("p").state.value == "HALF_OPEN"
    assert not cb.allow("p")
    cb.record_success("p")
    assert cb.allow("p")


@pytest.mark.asyncio
async def test_17_multiple_agents_fail(app: AetherApp):
    """When several agents fail, every one of them is actually tried."""
    mock = app.registry.get("mock")
    mock_b = app.registry.get("mock_b")
    mock.behavior["fail_on_calls"] = [1, 2]
    mock.behavior["fail_class"] = FailureClass.TIMEOUT.value
    mock_b.behavior["fail_on_calls"] = [1]
    mock_b.behavior["fail_class"] = FailureClass.CRASH.value

    task = await app.orchestrator.submit("Survive multi-agent failures")
    done = await wait_terminal(app, task.task_id, timeout=45)

    events = await app.repo.list_events(task.task_id)
    assigned = {
        e.detail.rsplit("\u2192", 1)[-1].split("(")[0].strip()
        for e in events
        if e.action == "assigned"
    }
    assert {"mock", "mock_b"} <= assigned, f"not every agent was tried: {assigned}"
    assert _count(events, "failover") >= 2, "failures did not each cause a failover"
    assert done.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}


@pytest.mark.asyncio
async def test_18_cancel_mid_flight(app: AetherApp):
    mock = app.registry.get("mock")
    assert isinstance(mock, MockAdapter)
    mock.behavior["delay"] = 3.0
    task = await app.orchestrator.submit("Long cancel test")
    await asyncio.sleep(0.2)
    t = await app.orchestrator.cancel_task(task.task_id)
    assert t.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_unavailable_adapter_does_not_fake_success():
    cfg = ProviderConfig(
        provider_id="hermes",
        display_name="Hermes",
        adapter=AdapterType.UNAVAILABLE,
        enabled=True,
        status_note="ADAPTER_REQUIRED",
    )
    adapter = UnavailableAdapter(cfg)
    health = await adapter.health_check()
    assert not health.available
    result = await adapter.execute(ExecutionRequest(task_id="T", provider_id="hermes", prompt="x"))
    assert not result.success
