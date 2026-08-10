"""Context manager — builds scoped context packages for agents."""

from __future__ import annotations

from typing import Optional

from aether.domain.enums import AgentRole
from aether.domain.models import ContextPackage, HandoffReport, Subtask, Task, TaskMemory
from aether.storage.repository import SqliteRepository


class ContextManager:
    def __init__(self, repo: SqliteRepository) -> None:
        self.repo = repo

    async def build_package(
        self,
        task: Task,
        memory: TaskMemory,
        assignment: str,
        role: AgentRole = AgentRole.GENERAL,
        subtask: Optional[Subtask] = None,
        handoff: Optional[HandoffReport] = None,
        checkpoint_id: Optional[str] = None,
        constraints: Optional[list[str]] = None,
    ) -> ContextPackage:
        if handoff is None:
            handoff = await self.repo.latest_handoff(task.task_id)

        decisions = [d.text for d in memory.decisions[-20:]]
        files = list(memory.files_index[-100:])
        issues = list(memory.known_issues[-20:])

        pkg = ContextPackage(
            task_id=task.task_id,
            goal=memory.goal or task.normalized_goal or task.original_prompt,
            current_state=memory.current_state,
            decisions=decisions,
            files=files,
            known_issues=issues,
            last_handoff=handoff,
            checkpoint_id=checkpoint_id,
            your_assignment=assignment,
            role=role,
            constraints=constraints
            or [
                "Work only within the assigned workspace.",
                "Do not invent secrets or credentials.",
                "Produce a structured HANDOFF REPORT when finished.",
                f"Approval level: {task.approval_level.value}",
            ],
            subtask=subtask,
            summary=memory.summary,
        )
        return pkg
