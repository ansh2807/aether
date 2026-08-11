"""WO-18 — exercise the storage layer for real against Postgres.

README claims "SQLite (SQLAlchemy async) — portable to Postgres". This module
is the proof: it drives `SqliteRepository` (the name is legacy — see its
docstring; nothing in it is SQLite-specific) through the same operations
`tests/test_parallel_state.py` exercises against SQLite, but over a real
`postgresql+asyncpg://` engine.

There is no Docker and no Postgres on the machine these tests were written
on, so the whole module is gated behind `AETHER_TEST_POSTGRES_URL` and SKIPS
(not errors, not xfails) when that isn't set. It only actually runs in CI's
`postgres` job (see `.github/workflows/ci.yml`), which starts a `postgres:16`
service container and points the env var at it. Nothing in this file has
been executed against a live database locally — it is written and
ruff-clean, but unverified until CI runs it.
"""

from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio

from aether.domain.enums import HandoffStatus, SubtaskStatus, TaskMode, TaskStatus
from aether.domain.models import Checkpoint, HandoffReport, Subtask, Task, TaskMemory, TimelineEvent
from aether.storage.db import Base, Database
from aether.storage.repository import SqliteRepository

pytestmark = pytest.mark.skipif(
    not os.environ.get("AETHER_TEST_POSTGRES_URL"),
    reason="set AETHER_TEST_POSTGRES_URL to run Postgres tests",
)


@pytest_asyncio.fixture
async def pg_repo():
    """A repository over a real Postgres engine, with a clean schema per test.

    Each test gets `create_all()` at setup and `drop_all()` at teardown so
    tests in this module never see each other's rows — important because,
    unlike the SQLite fixtures elsewhere, this database is a shared service
    container (or a developer's persistent local instance), not a fresh
    tempfile per test.
    """
    url = os.environ["AETHER_TEST_POSTGRES_URL"]
    db = Database(url)
    await db.create_all()
    repo = SqliteRepository(db)
    try:
        yield repo
    finally:
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await db.dispose()


@pytest.mark.asyncio
async def test_save_and_get_task_round_trips(pg_repo: SqliteRepository):
    task = Task(original_prompt="build a thing", mode=TaskMode.SOLO)
    await pg_repo.save_task(task)

    fetched = await pg_repo.get_task(task.task_id)
    assert fetched is not None
    assert fetched.task_id == task.task_id
    assert fetched.original_prompt == "build a thing"
    assert fetched.status == TaskStatus.CREATED

    # save_task again (the update path, row already exists) must overwrite
    # in place, not duplicate.
    task.status = TaskStatus.RUNNING
    task.progress = 0.5
    await pg_repo.save_task(task)

    refetched = await pg_repo.get_task(task.task_id)
    assert refetched is not None
    assert refetched.status == TaskStatus.RUNNING
    assert refetched.progress == 0.5

    all_tasks = await pg_repo.list_tasks()
    assert [t.task_id for t in all_tasks] == [task.task_id]


@pytest.mark.asyncio
async def test_list_tasks_filters_by_status(pg_repo: SqliteRepository):
    running = Task(original_prompt="a", status=TaskStatus.RUNNING)
    completed = Task(original_prompt="b", status=TaskStatus.COMPLETED)
    await pg_repo.save_task(running)
    await pg_repo.save_task(completed)

    only_running = await pg_repo.list_tasks(status=TaskStatus.RUNNING)
    assert {t.task_id for t in only_running} == {running.task_id}


@pytest.mark.asyncio
async def test_update_task_locked_read_modify_write(pg_repo: SqliteRepository):
    """`update_task` must read-modify-write under its per-task lock, not
    silently drop concurrent mutations the way two independent
    get()->mutate->save_task() calls would.
    """
    task = Task(original_prompt="lockstep")
    await pg_repo.save_task(task)

    def _bump(t: Task) -> None:
        t.progress = round(t.progress + 0.1, 5)

    await asyncio.gather(*[pg_repo.update_task(task.task_id, _bump) for _ in range(5)])

    final = await pg_repo.get_task(task.task_id)
    assert final is not None
    assert round(final.progress, 5) == 0.5


@pytest.mark.asyncio
async def test_concurrent_updates_do_not_lose_sibling_writes(pg_repo: SqliteRepository):
    """Postgres counterpart of
    tests/test_parallel_state.py::test_concurrent_updates_do_not_lose_sibling_writes.

    Ten writers racing on one task document, each going through the locked
    `update_task`, must all land — this is the property most likely to
    differ between SQLite (single-writer, effectively serialized) and
    Postgres (genuinely concurrent connections), so it gets its own
    dedicated test rather than trusting the SQLite result to generalize.
    """
    task = Task(original_prompt="race", mode=TaskMode.PARALLEL)
    for i in range(10):
        task.subtasks.append(Subtask(task_id=task.task_id, title=f"s{i}", order=i))
    await pg_repo.save_task(task)

    async def finish(subtask_id: str, label: str) -> None:
        def _apply(t: Task) -> None:
            s = next(x for x in t.subtasks if x.subtask_id == subtask_id)
            s.status = SubtaskStatus.COMPLETED
            s.result_summary = label
            if label not in t.participating_agents:
                t.participating_agents.append(label)

        await pg_repo.update_task(task.task_id, _apply)

    await asyncio.gather(*[finish(s.subtask_id, f"agent-{i}") for i, s in enumerate(task.subtasks)])

    stored = await pg_repo.get_task(task.task_id)
    assert stored is not None
    assert all(s.status == SubtaskStatus.COMPLETED for s in stored.subtasks)
    assert {s.result_summary for s in stored.subtasks} == {f"agent-{i}" for i in range(10)}
    # list-appends from every writer survived too
    assert len(stored.participating_agents) == 10


@pytest.mark.asyncio
async def test_memory_round_trip(pg_repo: SqliteRepository):
    task = Task(original_prompt="memo")
    await pg_repo.save_task(task)

    memory = TaskMemory(
        task_id=task.task_id,
        goal="ship the thing",
        current_state="in_progress",
        files_index=["src/a.py", "src/b.py"],
        last_action="wrote tests",
    )
    await pg_repo.save_memory(memory)

    fetched = await pg_repo.get_memory(task.task_id)
    assert fetched is not None
    assert fetched.goal == "ship the thing"
    assert fetched.files_index == ["src/a.py", "src/b.py"]

    # overwrite path
    memory.current_state = "done"
    await pg_repo.save_memory(memory)
    refetched = await pg_repo.get_memory(task.task_id)
    assert refetched is not None
    assert refetched.current_state == "done"


@pytest.mark.asyncio
async def test_timeline_round_trip(pg_repo: SqliteRepository):
    task = Task(original_prompt="timeline")
    await pg_repo.save_task(task)

    for i in range(3):
        await pg_repo.append_event(
            TimelineEvent(task_id=task.task_id, actor="orchestrator", action=f"step-{i}", detail=f"detail {i}")
        )

    events = await pg_repo.list_events(task.task_id)
    assert [e.action for e in events] == ["step-0", "step-1", "step-2"]


@pytest.mark.asyncio
async def test_handoff_round_trip(pg_repo: SqliteRepository):
    task = Task(original_prompt="handoff")
    await pg_repo.save_task(task)

    handoff = HandoffReport(
        task_id=task.task_id,
        from_agent="mock",
        status=HandoffStatus.PARTIAL,
        requested="implement feature",
        completed=["wrote skeleton"],
        remaining=["add tests"],
    )
    await pg_repo.save_handoff(handoff)

    handoffs = await pg_repo.list_handoffs(task.task_id)
    assert len(handoffs) == 1
    assert handoffs[0].handoff_id == handoff.handoff_id
    assert handoffs[0].completed == ["wrote skeleton"]

    latest = await pg_repo.latest_handoff(task.task_id)
    assert latest is not None
    assert latest.handoff_id == handoff.handoff_id


@pytest.mark.asyncio
async def test_checkpoint_round_trip(pg_repo: SqliteRepository):
    task = Task(original_prompt="checkpoint")
    await pg_repo.save_task(task)

    memory = TaskMemory(task_id=task.task_id, goal="reach the checkpoint")
    cp1 = Checkpoint(task_id=task.task_id, state_summary="first", memory_snapshot=memory)
    cp2 = Checkpoint(task_id=task.task_id, state_summary="second", memory_snapshot=memory)
    await pg_repo.save_checkpoint(cp1)
    await pg_repo.save_checkpoint(cp2)

    checkpoints = await pg_repo.list_checkpoints(task.task_id)
    assert [c.state_summary for c in checkpoints] == ["first", "second"]

    latest = await pg_repo.latest_checkpoint(task.task_id)
    assert latest is not None
    assert latest.checkpoint_id == cp2.checkpoint_id
    assert latest.memory_snapshot is not None
    assert latest.memory_snapshot.goal == "reach the checkpoint"

    fetched = await pg_repo.get_checkpoint(cp1.checkpoint_id)
    assert fetched is not None
    assert fetched.state_summary == "first"
