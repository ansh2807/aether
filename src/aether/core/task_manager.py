"""Task manager — lifecycle and persistence for tasks."""

from __future__ import annotations

from typing import Callable, Optional

from aether.config.settings import AetherSettings
from aether.domain.enums import TASK_TRANSITIONS, ApprovalLevel, TaskMode, TaskStatus
from aether.domain.models import Task, TaskMemory, TimelineEvent, utcnow
from aether.storage.repository import SqliteRepository


class InvalidStateTransition(Exception):
    pass


class TaskManager:
    def __init__(self, settings: AetherSettings, repo: SqliteRepository) -> None:
        self.settings = settings
        self.repo = repo

    async def create_task(
        self,
        prompt: str,
        mode: Optional[TaskMode] = None,
        approval_level: Optional[ApprovalLevel] = None,
        priority: int = 50,
        metadata: Optional[dict] = None,
    ) -> Task:
        task = Task(
            original_prompt=prompt,
            normalized_goal=prompt.strip(),
            status=TaskStatus.CREATED,
            priority=priority,
            mode=mode or self.settings.orchestrator.default_mode,
            approval_level=approval_level or self.settings.orchestrator.default_approval_level,
            metadata=metadata or {},
        )
        ws = self.settings.workspace_root / task.task_id
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "artifacts").mkdir(exist_ok=True)
        (ws / "logs").mkdir(exist_ok=True)
        (ws / "checkpoints").mkdir(exist_ok=True)
        task.workspace_path = str(ws.resolve())
        await self.repo.save_task(task)

        memory = TaskMemory(
            task_id=task.task_id,
            goal=task.normalized_goal,
            current_state="Task created",
            next_action="Plan and decompose",
        )
        await self.repo.save_memory(memory)
        await self.timeline(task.task_id, "user", "task_created", prompt[:500])
        return task

    async def transition(self, task: Task, new_status: TaskStatus) -> Task:
        allowed = TASK_TRANSITIONS.get(task.status, set())
        if new_status not in allowed and new_status != task.status:
            raise InvalidStateTransition(f"{task.status.value} -> {new_status.value} not allowed")
        task.status = new_status
        task.updated_at = utcnow()
        if new_status == TaskStatus.COMPLETED:
            task.completed_at = utcnow()
            task.progress = 1.0
        await self.repo.save_task(task)
        await self.timeline(task.task_id, "orchestrator", "status", new_status.value)
        return task

    async def save(self, task: Task) -> Task:
        task.updated_at = utcnow()
        return await self.repo.save_task(task)

    async def update(self, task_id: str, mutate: Callable[[Task], None]) -> Task:
        """Concurrency-safe read-modify-write. Use instead of save() whenever
        subtasks may be running in parallel."""
        return await self.repo.update_task(task_id, mutate)

    async def get(self, task_id: str) -> Optional[Task]:
        return await self.repo.get_task(task_id)

    async def list(self, status: Optional[TaskStatus] = None, limit: int = 100) -> list[Task]:
        return await self.repo.list_tasks(status=status, limit=limit)

    async def pause(self, task_id: str) -> Task:
        task = await self._require(task_id)
        return await self.transition(task, TaskStatus.PAUSED)

    async def resume(self, task_id: str) -> Task:
        task = await self._require(task_id)
        if task.status != TaskStatus.PAUSED and task.status != TaskStatus.FAILED:
            # allow resume from WAITING too
            if task.status == TaskStatus.WAITING:
                return await self.transition(task, TaskStatus.QUEUED)
            if task.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
                raise InvalidStateTransition(f"Cannot resume {task.status.value}")
        return await self.transition(task, TaskStatus.QUEUED)

    async def cancel(self, task_id: str) -> Task:
        task = await self._require(task_id)
        if task.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
            return task
        # force cancel even if not in allowed set for terminal safety
        task.status = TaskStatus.CANCELLED
        task.updated_at = utcnow()
        await self.repo.save_task(task)
        await self.timeline(task_id, "user", "cancelled", "Task cancelled by user")
        return task

    async def timeline(self, task_id: str, actor: str, action: str, detail: str = "") -> TimelineEvent:
        event = TimelineEvent(task_id=task_id, actor=actor, action=action, detail=detail)
        return await self.repo.append_event(event)

    async def _require(self, task_id: str) -> Task:
        task = await self.repo.get_task(task_id)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")
        return task
