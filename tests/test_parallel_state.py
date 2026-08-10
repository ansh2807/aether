"""Concurrent subtasks must not clobber each other's slice of the task document."""

from __future__ import annotations

import asyncio

import pytest

from aether.app import AetherApp
from aether.domain.enums import SubtaskStatus, TaskMode, TaskStatus
from aether.domain.models import Subtask, Task


async def wait_terminal(app: AetherApp, task_id: str, timeout: float = 40.0) -> Task:
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
async def test_concurrent_updates_do_not_lose_sibling_writes(app: AetherApp):
    """Ten writers racing on one task document must all land."""
    task = await app.tasks.create_task("race", mode=TaskMode.PARALLEL)
    for i in range(10):
        task.subtasks.append(Subtask(task_id=task.task_id, title=f"s{i}", order=i))
    await app.tasks.save(task)

    async def finish(subtask_id: str, label: str) -> None:
        def _apply(t: Task) -> None:
            s = next(x for x in t.subtasks if x.subtask_id == subtask_id)
            s.status = SubtaskStatus.COMPLETED
            s.result_summary = label
            if label not in t.participating_agents:
                t.participating_agents.append(label)

        await app.tasks.update(task.task_id, _apply)

    await asyncio.gather(
        *[finish(s.subtask_id, f"agent-{i}") for i, s in enumerate(task.subtasks)]
    )

    stored = await app.tasks.get(task.task_id)
    assert stored is not None
    assert all(s.status == SubtaskStatus.COMPLETED for s in stored.subtasks)
    assert {s.result_summary for s in stored.subtasks} == {f"agent-{i}" for i in range(10)}
    # list-appends from every writer survived too
    assert len(stored.participating_agents) == 10


@pytest.mark.asyncio
async def test_parallel_mode_completes_all_subtasks(app: AetherApp):
    task = await app.orchestrator.submit("Build a service", mode=TaskMode.PARALLEL)
    done = await wait_terminal(app, task.task_id)
    assert done.status == TaskStatus.COMPLETED
    unfinished = [
        s.title
        for s in done.subtasks
        if s.status not in (SubtaskStatus.COMPLETED, SubtaskStatus.SKIPPED)
    ]
    assert not unfinished, f"parallel subtasks left behind: {unfinished}"
    assert done.progress == 1.0


@pytest.mark.asyncio
async def test_stranded_running_subtask_is_reclaimed(app: AetherApp):
    """A subtask left RUNNING by a dead runner must not hang the lifecycle."""
    task = await app.tasks.create_task("Build a small tool")
    # simulate a runner that died mid-subtask, leaving it RUNNING and unowned
    task.subtasks = [
        Subtask(
            task_id=task.task_id,
            title="Implement",
            description="Implement the tool",
            status=SubtaskStatus.RUNNING,
        )
    ]
    await app.tasks.save(task)
    assert not app.orchestrator._inflight.get(task.task_id)

    await app.orchestrator.start_task(task.task_id)
    done = await wait_terminal(app, task.task_id)
    assert done.status == TaskStatus.COMPLETED

    events = await app.repo.list_events(task.task_id)
    assert any(e.action == "reclaimed" for e in events)


@pytest.mark.asyncio
async def test_unsatisfiable_dependency_fails_instead_of_hanging(app: AetherApp):
    task = await app.tasks.create_task("stuck")
    task.subtasks = [
        Subtask(
            task_id=task.task_id,
            title="never ready",
            depends_on=["sub-does-not-exist"],
            status=SubtaskStatus.PENDING,
        )
    ]
    await app.tasks.save(task)

    await app.orchestrator.start_task(task.task_id)
    done = await wait_terminal(app, task.task_id, timeout=15)
    assert done.status == TaskStatus.FAILED
    assert "Deadlocked" in done.failure_reason
