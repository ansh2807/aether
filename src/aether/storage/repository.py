"""SQLite-backed repositories implementing store interfaces."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import select

from aether.domain.enums import TaskStatus
from aether.domain.models import (
    AgentMessage,
    Checkpoint,
    HandoffReport,
    Task,
    TaskMemory,
    TimelineEvent,
    UsageSnapshot,
    VerificationResult,
)
from aether.security.redaction import SecretRedactor
from aether.storage.db import Database
from aether.storage.models import (
    CheckpointRow,
    HandoffRow,
    MemoryRow,
    MessageRow,
    TaskRow,
    TimelineRow,
    UsageRow,
    VerificationRow,
)


def _dump(model: Any) -> str:
    if hasattr(model, "model_dump_json"):
        return model.model_dump_json()
    return json.dumps(model)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SqliteRepository:
    """Unified persistence for tasks, memory, messages, checkpoints, etc."""

    def __init__(self, db: Database, redactor: SecretRedactor | None = None) -> None:
        self.db = db
        self.redactor = redactor or SecretRedactor()
        self._task_locks: dict[str, asyncio.Lock] = {}

    def task_lock(self, task_id: str) -> asyncio.Lock:
        lock = self._task_locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self._task_locks[task_id] = lock
        return lock

    async def update_task(self, task_id: str, mutate: Callable[[Task], None]) -> Task:
        """Read-modify-write a task under a per-task lock.

        A task is persisted as one JSON document, so concurrent subtasks doing
        get() → mutate → save_task() silently drop each other's writes. Every
        concurrent writer must go through here instead.
        """
        async with self.task_lock(task_id):
            task = await self.get_task(task_id)
            if task is None:
                raise KeyError(task_id)
            mutate(task)
            return await self.save_task(task)

    async def save_task(self, task: Task) -> Task:
        task.updated_at = _now()
        async with self.db.session() as session:
            row = await session.get(TaskRow, task.task_id)
            payload = task.model_dump_json()
            if row is None:
                session.add(
                    TaskRow(
                        task_id=task.task_id,
                        payload_json=payload,
                        status=task.status.value,
                        priority=task.priority,
                        progress=task.progress,
                        created_at=task.created_at,
                        updated_at=task.updated_at,
                    )
                )
            else:
                row.payload_json = payload
                row.status = task.status.value
                row.priority = task.priority
                row.progress = task.progress
                row.updated_at = task.updated_at
        return task

    async def get_task(self, task_id: str) -> Optional[Task]:
        async with self.db.session() as session:
            row = await session.get(TaskRow, task_id)
            if row is None:
                return None
            return Task.model_validate_json(row.payload_json)

    async def list_tasks(self, status: Optional[TaskStatus] = None, limit: int = 100) -> list[Task]:
        async with self.db.session() as session:
            stmt = select(TaskRow).order_by(TaskRow.updated_at.desc()).limit(limit)
            if status is not None:
                stmt = stmt.where(TaskRow.status == status.value)
            rows = (await session.execute(stmt)).scalars().all()
            return [Task.model_validate_json(r.payload_json) for r in rows]

    async def update_status(self, task_id: str, status: TaskStatus) -> Task:
        task = await self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        task.status = status
        task.updated_at = _now()
        if status == TaskStatus.COMPLETED:
            task.completed_at = _now()
            task.progress = 1.0
        return await self.save_task(task)

    async def delete_task(self, task_id: str) -> None:
        async with self.db.session() as session:
            row = await session.get(TaskRow, task_id)
            if row:
                await session.delete(row)

    async def save_memory(self, memory: TaskMemory) -> TaskMemory:
        memory.updated_at = _now()
        async with self.db.session() as session:
            row = await session.get(MemoryRow, memory.task_id)
            payload = memory.model_dump_json()
            if row is None:
                session.add(
                    MemoryRow(task_id=memory.task_id, payload_json=payload, updated_at=memory.updated_at)
                )
            else:
                row.payload_json = payload
                row.updated_at = memory.updated_at
        return memory

    async def get_memory(self, task_id: str) -> Optional[TaskMemory]:
        async with self.db.session() as session:
            row = await session.get(MemoryRow, task_id)
            if row is None:
                return None
            return TaskMemory.model_validate_json(row.payload_json)

    async def append_message(self, message: AgentMessage) -> AgentMessage:
        message.content = self.redactor.redact(message.content)
        async with self.db.session() as session:
            session.add(
                MessageRow(
                    message_id=message.message_id,
                    task_id=message.task_id,
                    payload_json=message.model_dump_json(),
                    timestamp=message.timestamp,
                )
            )
        return message

    async def list_messages(self, task_id: str, limit: int = 500) -> list[AgentMessage]:
        async with self.db.session() as session:
            stmt = (
                select(MessageRow)
                .where(MessageRow.task_id == task_id)
                .order_by(MessageRow.timestamp.asc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [AgentMessage.model_validate_json(r.payload_json) for r in rows]

    async def save_checkpoint(self, checkpoint: Checkpoint) -> Checkpoint:
        async with self.db.session() as session:
            session.add(
                CheckpointRow(
                    checkpoint_id=checkpoint.checkpoint_id,
                    task_id=checkpoint.task_id,
                    payload_json=checkpoint.model_dump_json(),
                    created_at=checkpoint.created_at,
                )
            )
        return checkpoint

    async def list_checkpoints(self, task_id: str) -> list[Checkpoint]:
        async with self.db.session() as session:
            stmt = (
                select(CheckpointRow)
                .where(CheckpointRow.task_id == task_id)
                .order_by(CheckpointRow.created_at.asc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [Checkpoint.model_validate_json(r.payload_json) for r in rows]

    async def get_checkpoint(self, checkpoint_id: str) -> Optional[Checkpoint]:
        async with self.db.session() as session:
            row = await session.get(CheckpointRow, checkpoint_id)
            if row is None:
                return None
            return Checkpoint.model_validate_json(row.payload_json)

    async def latest_checkpoint(self, task_id: str) -> Optional[Checkpoint]:
        items = await self.list_checkpoints(task_id)
        return items[-1] if items else None

    async def save_handoff(self, handoff: HandoffReport) -> HandoffReport:
        async with self.db.session() as session:
            session.add(
                HandoffRow(
                    handoff_id=handoff.handoff_id,
                    task_id=handoff.task_id,
                    payload_json=handoff.model_dump_json(),
                    created_at=handoff.created_at,
                )
            )
        return handoff

    async def list_handoffs(self, task_id: str) -> list[HandoffReport]:
        async with self.db.session() as session:
            stmt = (
                select(HandoffRow)
                .where(HandoffRow.task_id == task_id)
                .order_by(HandoffRow.created_at.asc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [HandoffReport.model_validate_json(r.payload_json) for r in rows]

    async def latest_handoff(self, task_id: str) -> Optional[HandoffReport]:
        items = await self.list_handoffs(task_id)
        return items[-1] if items else None

    async def append_event(self, event: TimelineEvent) -> TimelineEvent:
        event.detail = self.redactor.redact(event.detail)
        async with self.db.session() as session:
            session.add(
                TimelineRow(
                    event_id=event.event_id,
                    task_id=event.task_id,
                    payload_json=event.model_dump_json(),
                    timestamp=event.timestamp,
                )
            )
        return event

    async def list_events(self, task_id: str, limit: int = 500) -> list[TimelineEvent]:
        async with self.db.session() as session:
            stmt = (
                select(TimelineRow)
                .where(TimelineRow.task_id == task_id)
                .order_by(TimelineRow.timestamp.asc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [TimelineEvent.model_validate_json(r.payload_json) for r in rows]

    async def save_result(self, result: VerificationResult) -> VerificationResult:
        async with self.db.session() as session:
            session.add(
                VerificationRow(
                    result_id=result.result_id,
                    task_id=result.task_id,
                    payload_json=result.model_dump_json(),
                    created_at=result.created_at,
                )
            )
        return result

    async def list_results(self, task_id: str) -> list[VerificationResult]:
        async with self.db.session() as session:
            stmt = (
                select(VerificationRow)
                .where(VerificationRow.task_id == task_id)
                .order_by(VerificationRow.created_at.asc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [VerificationResult.model_validate_json(r.payload_json) for r in rows]

    async def save_usage(self, usage: UsageSnapshot) -> UsageSnapshot:
        async with self.db.session() as session:
            row = await session.get(UsageRow, usage.provider_id)
            payload = usage.model_dump_json()
            if row is None:
                session.add(UsageRow(provider_id=usage.provider_id, payload_json=payload))
            else:
                row.payload_json = payload
        return usage

    async def get_usage(self, provider_id: str) -> Optional[UsageSnapshot]:
        async with self.db.session() as session:
            row = await session.get(UsageRow, provider_id)
            if row is None:
                return None
            return UsageSnapshot.model_validate_json(row.payload_json)


class FileAuditLogger:
    def __init__(self, path: Path, redactor: SecretRedactor | None = None) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor or SecretRedactor()

    async def log(self, action: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            **fields,
        }
        line = json.dumps(record, default=str)
        line = self.redactor.redact(line)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
