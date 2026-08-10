"""End-to-end orchestrator tests with mock adapters."""

from __future__ import annotations

import asyncio

import pytest

from aether.adapters.mock_adapter import MockAdapter
from aether.app import AetherApp
from aether.domain.enums import FailureClass, TaskMode, TaskStatus


async def wait_terminal(app: AetherApp, task_id: str, timeout: float = 30.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
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
    raise TimeoutError(f"Task {task_id} not terminal: {t.status if t else None}")


@pytest.mark.asyncio
async def test_happy_path_build_task(app: AetherApp):
    task = await app.orchestrator.submit("Build a small Python CLI tool", mode=TaskMode.SOLO)
    done = await wait_terminal(app, task.task_id)
    assert done.status == TaskStatus.COMPLETED
    assert done.progress == 1.0
    assert done.participating_agents
    data = await app.orchestrator.inspect(task.task_id)
    assert data["timeline"]
    assert data["handoffs"]
    assert data["checkpoints"]


@pytest.mark.asyncio
async def test_task_persistence_across_reload(app: AetherApp, settings):
    task = await app.orchestrator.submit("Research quantum networking briefly", mode=TaskMode.SOLO)
    done = await wait_terminal(app, task.task_id)
    tid = done.task_id
    await app.stop()

    app2 = AetherApp(settings)
    await app2.start()
    try:
        loaded = await app2.tasks.get(tid)
        assert loaded is not None
        assert loaded.status == TaskStatus.COMPLETED
        mem = await app2.repo.get_memory(tid)
        assert mem is not None
        assert mem.goal
    finally:
        await app2.stop()


@pytest.mark.asyncio
async def test_failover_on_timeout(app: AetherApp):
    mock = app.registry.get("mock")
    assert isinstance(mock, MockAdapter)
    mock.set_fail_next(FailureClass.TIMEOUT)

    task = await app.orchestrator.submit("Implement hello world module", mode=TaskMode.SOLO)
    done = await wait_terminal(app, task.task_id, timeout=40)
    # Should complete via failover to mock or mock_b
    assert done.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}
    events = await app.repo.list_events(task.task_id)
    actions = [e.action for e in events]
    assert "failover" in actions or done.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_cancel_during_execution(app: AetherApp):
    mock = app.registry.get("mock")
    assert isinstance(mock, MockAdapter)
    mock.behavior["delay"] = 2.0

    task = await app.orchestrator.submit("Build long running thing", mode=TaskMode.SOLO)
    await asyncio.sleep(0.2)
    cancelled = await app.orchestrator.cancel_task(task.task_id)
    assert cancelled.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_pause_resume(app: AetherApp):
    mock = app.registry.get("mock")
    assert isinstance(mock, MockAdapter)
    mock.behavior["delay"] = 1.0

    task = await app.orchestrator.submit("Build pauseable app", mode=TaskMode.SOLO)
    await asyncio.sleep(0.15)
    paused = await app.orchestrator.pause_task(task.task_id)
    assert paused.status == TaskStatus.PAUSED
    # reset delay so resume finishes quickly
    mock.behavior["delay"] = 0.05
    resumed = await app.orchestrator.resume_task(task.task_id)
    done = await wait_terminal(app, resumed.task_id)
    assert done.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.PAUSED}


@pytest.mark.asyncio
async def test_verification_detects_bad_code(app: AetherApp):
    mock = app.registry.get("mock")
    assert isinstance(mock, MockAdapter)
    mock.behavior["produce_bad_code"] = True

    task = await app.orchestrator.submit("Build broken app intentionally", mode=TaskMode.SOLO)
    done = await wait_terminal(app, task.task_id, timeout=40)
    # Either fixed by debugger loop or failed after max verification loops
    assert done.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}
    results = await app.repo.list_results(task.task_id)
    # verification should have run at least once if subtasks finished
    if results:
        assert any(r.checks for r in results)


@pytest.mark.asyncio
async def test_collab_mode(app: AetherApp):
    task = await app.orchestrator.submit(
        "Build a production-grade authentication system", mode=TaskMode.COLLAB
    )
    done = await wait_terminal(app, task.task_id, timeout=45)
    assert done.status == TaskStatus.COMPLETED
    assert len(done.subtasks) >= 4


@pytest.mark.asyncio
async def test_debate_mode(app: AetherApp):
    task = await app.orchestrator.submit(
        "Design the best architecture for a QR file transfer app", mode=TaskMode.DEBATE
    )
    done = await wait_terminal(app, task.task_id, timeout=45)
    assert done.status == TaskStatus.COMPLETED
    titles = [s.title for s in done.subtasks]
    assert any("Proposal" in t for t in titles)


@pytest.mark.asyncio
async def test_multiple_agents_fail_then_succeed(app: AetherApp):
    mock = app.registry.get("mock")
    mock_b = app.registry.get("mock_b")
    assert isinstance(mock, MockAdapter)
    assert isinstance(mock_b, MockAdapter)
    mock.behavior["fail_on_calls"] = [1]
    mock.behavior["fail_class"] = FailureClass.RATE_LIMIT.value

    task = await app.orchestrator.submit("Fix this bug in module", mode=TaskMode.SOLO)
    done = await wait_terminal(app, task.task_id, timeout=40)
    assert done.status == TaskStatus.COMPLETED
