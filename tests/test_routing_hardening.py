"""WO-23 and WO-24: two gaps found by comparing AETHER's router to a gateway.

WO-23 — a revoked API key was retried forever. `record_failure` took only a
provider id, so the breaker could not tell an expired credential from a network
blip: it opened for 60s, half-opened, spent a real subtask attempt on a request
that could not succeed, and repeated indefinitely.

WO-24 — selection ignored work already in flight, so N subtasks dispatched
together scored identically and all chose the same provider, queueing behind one
rate limit while healthy providers idled.
"""

from __future__ import annotations

import asyncio

import pytest

from aether.config.settings import CircuitBreakerSettings
from aether.core.circuit import CircuitBreaker
from aether.domain.enums import CircuitState, FailureClass


def _breaker(threshold: int = 5, recovery: int = 60) -> CircuitBreaker:
    return CircuitBreaker(
        CircuitBreakerSettings(
            failure_threshold=threshold,
            recovery_timeout_seconds=recovery,
            half_open_max_calls=1,
        )
    )


# --- WO-23: terminal failures ------------------------------------------


def test_auth_failure_disables_after_a_single_attempt():
    """The acceptance criterion: attempts, not elapsed time.

    Asserting "still blocked after 60s" would pass against the old code too if
    the clock had not advanced. Counting attempts cannot be faked by timing.
    """
    cb = _breaker(threshold=5)
    assert cb.allow("p")

    cb.record_failure("p", FailureClass.AUTH)

    snap = cb.snapshot("p")
    assert snap.state == CircuitState.DISABLED, (
        f"state={snap.state}; a revoked credential must not sit in a retry loop"
    )
    assert snap.failure_count == 1, "should not have burned the failure threshold first"
    assert not cb.allow("p")


def test_a_disabled_provider_never_reopens_on_a_timer():
    """OPEN is a timer; DISABLED must not be. Recovery timeout of zero means
    an OPEN circuit would half-open immediately — DISABLED must not."""
    cb = _breaker(recovery=0)

    cb.record_failure("timer", FailureClass.TIMEOUT)
    for _ in range(4):
        cb.record_failure("timer", FailureClass.TIMEOUT)
    assert cb.snapshot("timer").state == CircuitState.OPEN
    assert cb.allow("timer"), "an OPEN circuit with 0s recovery should probe"

    cb.record_failure("auth", FailureClass.AUTH)
    assert not cb.allow("auth"), "DISABLED reopened on a timer"
    assert not cb.allow("auth")
    assert not cb.allow("auth")


def test_disabled_carries_an_actionable_reason():
    """And the action it names must be one that actually works.

    An earlier version pointed at `aether providers reset <id>`. Breaker state
    is in-memory and never persisted, so a separate CLI process would have
    operated on its own empty breaker and done nothing at all — instructions
    that quietly fail are worse than none.
    """
    cb = _breaker()
    cb.record_failure("openai", FailureClass.AUTH)
    reason = cb.snapshot("openai").disabled_reason
    assert reason and "AUTH" in reason
    assert "aether connect openai" in reason, "the operator must be told what to do"
    assert "providers reset" not in reason, (
        "points at an out-of-process command that cannot affect this breaker"
    )


def test_reset_is_the_only_way_back():
    cb = _breaker()
    cb.record_failure("p", FailureClass.AUTH)
    assert not cb.allow("p")

    cb.reset("p")
    assert cb.allow("p")
    snap = cb.snapshot("p")
    assert snap.state == CircuitState.CLOSED
    assert snap.disabled_reason is None
    assert snap.failure_count == 0


@pytest.mark.parametrize(
    "failure",
    [
        FailureClass.RATE_LIMIT,
        FailureClass.QUOTA,
        FailureClass.TIMEOUT,
        FailureClass.NETWORK,
        FailureClass.CRASH,
        FailureClass.CONTEXT,
        FailureClass.VERIFICATION,
        FailureClass.INVALID_RESPONSE,
        FailureClass.MALFORMED_HANDOFF,
        FailureClass.UNKNOWN,
    ],
)
def test_transient_failures_are_not_made_terminal(failure: FailureClass):
    """Over-applying terminality takes a healthy provider out of rotation until
    a human notices, which is its own outage. Rate limits especially recover on
    a clock — that is what OPEN is for."""
    cb = _breaker(threshold=5)
    cb.record_failure("p", failure)
    assert cb.snapshot("p").state != CircuitState.DISABLED, (
        f"{failure.value} was treated as terminal; waiting does fix it"
    )
    assert cb.allow("p"), f"a single {failure.value} should not stop routing"


def test_rate_limited_provider_still_returns_after_cooldown():
    """The mirror of the AUTH test: prove we did not over-apply terminality."""
    cb = _breaker(threshold=2, recovery=0)
    cb.record_failure("p", FailureClass.RATE_LIMIT)
    cb.record_failure("p", FailureClass.RATE_LIMIT)
    assert cb.snapshot("p").state == CircuitState.OPEN
    assert cb.allow("p"), "a rate-limited provider must come back"


def test_omitting_the_failure_class_keeps_the_old_behaviour():
    """The parameter is optional so existing callers are not silently changed."""
    cb = _breaker(threshold=5)
    cb.record_failure("p")
    assert cb.snapshot("p").state == CircuitState.CLOSED
    assert cb.allow("p")


def test_the_terminal_set_is_deliberate():
    """A guard on the set itself. Adding to it is a real decision — an
    over-eager entry silently removes a working provider."""
    assert CircuitBreaker.TERMINAL_FAILURES == frozenset(
        {FailureClass.AUTH, FailureClass.POLICY_DENIED}
    )


def test_every_orchestrator_and_chat_call_site_passes_a_class():
    """The bug was not the breaker — it was the call sites dropping the class
    they already had in scope. Nothing here catches that except this."""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "aether" / "core"
    bare = []
    for name in ("orchestrator.py", "chat_session.py"):
        for i, line in enumerate(src.joinpath(name).read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"circuit\.record_failure\([^,)]+\)\s*$", line):
                bare.append(f"{name}:{i}")
    assert not bare, f"record_failure called without a failure class at {bare}"


# --- WO-24: concurrent load ---------------------------------------------


def test_inflight_counter_tracks_dispatch_and_completion():
    from aether.core.router import AgentRouter

    r = AgentRouter.__new__(AgentRouter)          # scoring needs no wiring for this
    r._inflight = {}

    assert r.inflight("a") == 0
    r.note_dispatch("a")
    r.note_dispatch("a")
    assert r.inflight("a") == 2
    r.note_complete("a")
    assert r.inflight("a") == 1
    r.note_complete("a")
    assert r.inflight("a") == 0
    assert "a" not in r._inflight, "counter should not leak empty entries"


def test_completing_more_than_dispatched_does_not_go_negative():
    """A negative count would turn into a routing *bonus* for a provider that
    is not actually idle."""
    from aether.core.router import AgentRouter

    r = AgentRouter.__new__(AgentRouter)
    r._inflight = {}
    r.note_complete("a")
    r.note_complete("a")
    assert r.inflight("a") == 0


async def _three_equal_providers(tmp_path):
    """A router over three interchangeable providers.

    Equal capabilities on purpose: with the scores tied, load is the only thing
    that can separate them, so the test measures exactly the behaviour it names.
    """
    from aether.app import AetherApp
    from aether.config.settings import AetherSettings, OrchestratorSettings
    from aether.domain.enums import AdapterType
    from aether.domain.models import ProviderConfig

    caps = {"coding": 9, "reasoning": 9, "architecture": 9, "testing": 9, "review": 9}
    s = AetherSettings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "ws",
        database_url="sqlite+aiosqlite:///" + (tmp_path / "data" / "t.db").as_posix(),
        orchestrator=OrchestratorSettings(enable_git_checkpoints=False),
    )
    s.providers = {
        pid: ProviderConfig(
            provider_id=pid, display_name=pid, adapter=AdapterType.MOCK,
            enabled=True, for_testing=True, capabilities=dict(caps),
        )
        for pid in ("mock_a", "mock_b", "mock_c")
    }
    s.ensure_dirs()
    app = AetherApp(s)
    await app.start()
    return app


@pytest.mark.asyncio
async def test_eight_concurrent_subtasks_do_not_all_land_on_one_provider(tmp_path):
    """The WO-24 acceptance criterion, through the real `select()`.

    Measured before the fix: 8 of 8 on a single provider. The assertion is on
    spread rather than on an exact distribution, because the point is that work
    is shared, not that it is shared evenly.
    """
    from aether.domain.enums import TaskMode

    app = await _three_equal_providers(tmp_path)
    try:
        task = await app.orchestrator.submit("Build a thing", mode=TaskMode.SOLO,
                                             auto_start=False)
        chosen = []
        for _ in range(8):
            decision = await app.router.select(task)
            assert decision is not None
            chosen.append(decision.provider_id)
            # dispatched but not finished — exactly the parallel case
            app.router.note_dispatch(decision.provider_id)

        distinct = set(chosen)
        assert len(distinct) >= 2, (
            f"all 8 subtasks routed to {chosen[0]} — concurrent load was ignored"
        )
        busiest = max(chosen.count(p) for p in distinct)
        assert busiest < 8, "one provider absorbed everything"
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_a_single_subtask_still_picks_the_best_provider(tmp_path):
    """Spread must not cost selection quality when there is nothing to spread.

    With no work in flight the load term is zero, so the strongest provider
    wins — otherwise we would have traded a real property for a cosmetic one.
    """
    from aether.app import AetherApp
    from aether.config.settings import AetherSettings, OrchestratorSettings
    from aether.domain.enums import AdapterType, TaskMode
    from aether.domain.models import ProviderConfig

    s = AetherSettings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "ws",
        database_url="sqlite+aiosqlite:///" + (tmp_path / "data" / "t.db").as_posix(),
        orchestrator=OrchestratorSettings(enable_git_checkpoints=False),
    )
    s.providers = {
        "strong": ProviderConfig(
            provider_id="strong", display_name="strong", adapter=AdapterType.MOCK,
            enabled=True, for_testing=True,
            capabilities={"coding": 10, "reasoning": 10, "architecture": 10},
        ),
        "weak": ProviderConfig(
            provider_id="weak", display_name="weak", adapter=AdapterType.MOCK,
            enabled=True, for_testing=True,
            capabilities={"coding": 2, "reasoning": 2, "architecture": 2},
        ),
    }
    s.ensure_dirs()
    app = AetherApp(s)
    await app.start()
    try:
        task = await app.orchestrator.submit("x", mode=TaskMode.SOLO, auto_start=False)
        first = await app.router.select(task)
        assert first.provider_id == "strong"

        # one unit of load must not be enough to hand work to a far worse agent
        app.router.note_dispatch("strong")
        second = await app.router.select(task)
        assert second.provider_id == "strong", (
            "a single in-flight subtask outweighed an 8-point capability gap"
        )
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_the_orchestrator_actually_reports_dispatch(tmp_path):
    """Proves the wiring, not just the counter.

    Added because a mutation survived: deleting `note_dispatch` from
    `orchestrator.py` left every other test in this file green. They all call
    the counter by hand, so they exercise the router in isolation and say
    nothing about whether anything ever feeds it. Without this test, WO-24
    could stop working entirely and CI would not notice.
    """
    from aether.domain.enums import TaskMode

    app = await _three_equal_providers(tmp_path)
    try:
        observed: list[int] = []
        provider_id = "mock_a"
        adapter = app.registry.get(provider_id)
        adapter.behavior["delay"] = 0.2
        original = adapter.execute

        async def watched(req):
            # sampled while the provider is genuinely mid-execution
            observed.append(app.router.inflight(provider_id))
            return await original(req)

        adapter.execute = watched

        task = await app.orchestrator.submit("Build a thing", mode=TaskMode.SOLO,
                                             auto_start=False)
        await app.orchestrator.start_task(task.task_id)

        for _ in range(300):
            if observed:
                break
            await asyncio.sleep(0.1)

        assert observed, f"{provider_id} was never executed; cannot judge the wiring"
        assert max(observed) >= 1, (
            "the orchestrator never told the router it had dispatched work — "
            "the load term will always read zero in production"
        )

        await app.orchestrator.cancel_task(task.task_id)
        for _ in range(100):
            if app.router.inflight(provider_id) == 0:
                break
            await asyncio.sleep(0.1)
        assert app.router.inflight(provider_id) == 0, (
            "in-flight count leaked after execution finished"
        )
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_the_load_term_is_recorded_in_the_decision(tmp_path):
    """Routing decisions stay auditable — that is the orchestrator's job."""
    from aether.domain.enums import TaskMode

    app = await _three_equal_providers(tmp_path)
    try:
        task = await app.orchestrator.submit("x", mode=TaskMode.SOLO, auto_start=False)
        first = await app.router.select(task)
        app.router.note_dispatch(first.provider_id)

        # force the busy provider to be the one selected, so its reasons show load
        for _ in range(6):
            d = await app.router.select(task)
            if d.provider_id == first.provider_id:
                assert any("inflight=" in r for r in d.reasons), (
                    f"load not explained in {d.reasons}"
                )
                break
            app.router.note_dispatch(d.provider_id)
        else:
            pytest.fail("never re-selected the busy provider; cannot check its reasons")
    finally:
        await app.stop()


# --- routing decisions must be explicable after the fact -----------------


@pytest.mark.asyncio
async def test_the_timeline_records_why_a_provider_was_chosen(tmp_path):
    """`reasons` was computed on every selection and then thrown away.

    The `assigned` event said what was picked and never why, so "why did it use
    that provider, and what else was available?" was unanswerable after a run —
    including for the load term added in WO-24, whose changelog entry claimed
    routing stayed auditable when only tests could see it.
    """
    from aether.domain.enums import TaskMode

    app = await _three_equal_providers(tmp_path)
    try:
        task = await app.orchestrator.submit("Build a thing", mode=TaskMode.SOLO,
                                             auto_start=False)
        await app.orchestrator.start_task(task.task_id)

        assigned = []
        for _ in range(300):
            events = await app.repo.list_events(task.task_id)
            assigned = [e for e in events if e.action == "assigned"]
            if assigned:
                break
            await asyncio.sleep(0.1)

        assert assigned, "no assignment was ever recorded"
        detail = assigned[0].detail
        assert "score=" in detail, f"no score in {detail!r}"
        assert "capability:" in detail, f"no scoring factors in {detail!r}"
        assert "role=" in detail, "the pre-existing detail was lost"

        await app.orchestrator.cancel_task(task.task_id)
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_the_rejected_alternatives_are_recorded(tmp_path):
    """"Why not the other one" needs the other ones written down."""
    from aether.domain.enums import TaskMode

    app = await _three_equal_providers(tmp_path)
    try:
        task = await app.orchestrator.submit("Build a thing", mode=TaskMode.SOLO,
                                             auto_start=False)
        await app.orchestrator.start_task(task.task_id)

        for _ in range(300):
            events = await app.repo.list_events(task.task_id)
            assigned = [e for e in events if e.action == "assigned"]
            if assigned:
                break
            await asyncio.sleep(0.1)

        assert assigned
        assert " over " in assigned[0].detail, (
            f"the candidates that lost are not recorded: {assigned[0].detail!r}"
        )
        await app.orchestrator.cancel_task(task.task_id)
    finally:
        await app.stop()
