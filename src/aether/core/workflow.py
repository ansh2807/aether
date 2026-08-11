"""Workflow engine — plan DAG of subtasks for modes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from aether.domain.enums import AgentRole, SubtaskStatus, TaskMode
from aether.domain.models import Subtask, Task

if TYPE_CHECKING:
    from aether.config.settings import AetherSettings
    from aether.core.planner import PlanResult
    from aether.interfaces.adapters import AgentAdapter


class WorkflowEngine:
    """Deterministic planner — orchestrator-owned, not agent-owned.

    ``plan()`` stays synchronous and keyword-based — it is also used for the
    CLI's plan preview, which has no model call available. ``plan_task()`` is
    the async path (WO-11): it asks a model for a richer subtask DAG via
    ``LLMPlanner`` and falls back to this same keyword planner, unvalidated
    plans never surviving contact with the orchestrator.
    """

    def plan(self, task: Task) -> list[Subtask]:
        if task.mode == TaskMode.DEBATE:
            return self._plan_debate(task)
        if task.mode == TaskMode.COLLAB:
            return self._plan_collab(task)
        if task.mode == TaskMode.PARALLEL:
            return self._plan_parallel(task)
        return self._plan_solo(task)

    async def plan_task(
        self,
        task: Task,
        adapter: Optional["AgentAdapter"],
        settings: "AetherSettings",
    ) -> "PlanResult":
        """Async planning path: LLM planner with the keyword planner as fallback.

        ``adapter`` is the provider chosen to do the planning (or ``None`` when
        no real provider is available — the keyword planner is used
        immediately in that case). Only non-DEBATE/COLLAB/PARALLEL modes are
        eligible for LLM planning; those modes have fixed, deliberately
        structured shapes and keep using the deterministic planner.
        """
        from aether.core.planner import LLMPlanner

        if task.mode != TaskMode.SOLO:
            from aether.core.planner import PlanResult

            return PlanResult(self.plan(task), source="keyword", reason=f"mode={task.mode.value} is not LLM-planned")

        planner = LLMPlanner(settings)
        return await planner.plan_task(task, adapter, self.plan)

    def _plan_solo(self, task: Task) -> list[Subtask]:
        prompt = task.original_prompt.lower()
        subs: list[Subtask] = []
        s1 = Subtask(
            task_id=task.task_id,
            title="Plan approach",
            description=f"Analyze and plan: {task.original_prompt}",
            required_capabilities=["architecture", "reasoning"],
            role=AgentRole.ARCHITECT,
            order=0,
            status=SubtaskStatus.READY,
        )
        subs.append(s1)

        if any(k in prompt for k in ("build", "implement", "code", "app", "fix", "api", "bug", "fix")):
            s2 = Subtask(
                task_id=task.task_id,
                title="Implement",
                description=f"Implement solution for: {task.original_prompt}",
                required_capabilities=["coding", "terminal"],
                role=AgentRole.IMPLEMENTER,
                depends_on=[s1.subtask_id],
                order=1,
            )
            s3 = Subtask(
                task_id=task.task_id,
                title="Verify",
                description="Run verification and fix issues",
                required_capabilities=["testing", "coding"],
                role=AgentRole.TESTER,
                depends_on=[s2.subtask_id],
                order=2,
            )
            subs.extend([s2, s3])
        elif any(k in prompt for k in ("research", "analyze", "compare", "survey")):
            s2 = Subtask(
                task_id=task.task_id,
                title="Research",
                description=task.original_prompt,
                required_capabilities=["research", "reasoning"],
                role=AgentRole.RESEARCHER,
                depends_on=[s1.subtask_id],
                order=1,
            )
            subs.append(s2)
        else:
            s2 = Subtask(
                task_id=task.task_id,
                title="Execute",
                description=task.original_prompt,
                required_capabilities=["reasoning", "coding"],
                role=AgentRole.GENERAL,
                depends_on=[s1.subtask_id],
                order=1,
            )
            subs.append(s2)
        return subs

    def _plan_collab(self, task: Task) -> list[Subtask]:
        arch = Subtask(
            task_id=task.task_id,
            title="Architecture",
            description=f"Design architecture for: {task.original_prompt}",
            required_capabilities=["architecture"],
            role=AgentRole.ARCHITECT,
            order=0,
            status=SubtaskStatus.READY,
        )
        impl = Subtask(
            task_id=task.task_id,
            title="Implementation",
            description="Implement according to architecture",
            required_capabilities=["coding", "terminal"],
            role=AgentRole.IMPLEMENTER,
            depends_on=[arch.subtask_id],
            order=1,
        )
        sec = Subtask(
            task_id=task.task_id,
            title="Security review",
            description="Security review of implementation",
            required_capabilities=["review"],
            role=AgentRole.SECURITY_REVIEWER,
            depends_on=[impl.subtask_id],
            order=2,
        )
        test = Subtask(
            task_id=task.task_id,
            title="Testing",
            description="Test the implementation",
            required_capabilities=["testing"],
            role=AgentRole.TESTER,
            depends_on=[impl.subtask_id],
            order=2,
        )
        final = Subtask(
            task_id=task.task_id,
            title="Final review",
            description="Final review and recommendations",
            required_capabilities=["review", "architecture"],
            role=AgentRole.FINAL_REVIEWER,
            depends_on=[sec.subtask_id, test.subtask_id],
            order=3,
        )
        return [arch, impl, sec, test, final]

    def _plan_parallel(self, task: Task) -> list[Subtask]:
        arch = Subtask(
            task_id=task.task_id,
            title="Architecture",
            description=f"High-level design: {task.original_prompt}",
            required_capabilities=["architecture"],
            role=AgentRole.ARCHITECT,
            order=0,
            status=SubtaskStatus.READY,
        )
        # Independent workstreams after architecture
        db = Subtask(
            task_id=task.task_id,
            title="Database",
            description="Design and scaffold data layer",
            required_capabilities=["coding"],
            role=AgentRole.IMPLEMENTER,
            depends_on=[arch.subtask_id],
            order=1,
        )
        backend = Subtask(
            task_id=task.task_id,
            title="Backend",
            description="Implement backend services",
            required_capabilities=["coding", "terminal"],
            role=AgentRole.IMPLEMENTER,
            depends_on=[arch.subtask_id],
            order=1,
        )
        frontend = Subtask(
            task_id=task.task_id,
            title="Frontend",
            description="Implement frontend",
            required_capabilities=["coding"],
            role=AgentRole.IMPLEMENTER,
            depends_on=[arch.subtask_id],
            order=1,
        )
        testing = Subtask(
            task_id=task.task_id,
            title="Integration testing",
            description="Integration tests across components",
            required_capabilities=["testing"],
            role=AgentRole.TESTER,
            depends_on=[db.subtask_id, backend.subtask_id, frontend.subtask_id],
            order=2,
        )
        return [arch, db, backend, frontend, testing]

    def _plan_debate(self, task: Task) -> list[Subtask]:
        a = Subtask(
            task_id=task.task_id,
            title="Proposal A",
            description=f"Independent proposal A for: {task.original_prompt}",
            required_capabilities=["architecture", "reasoning"],
            role=AgentRole.ARCHITECT,
            order=0,
            status=SubtaskStatus.READY,
        )
        b = Subtask(
            task_id=task.task_id,
            title="Proposal B",
            description=f"Independent proposal B for: {task.original_prompt}",
            required_capabilities=["architecture", "reasoning"],
            role=AgentRole.ARCHITECT,
            order=0,
            status=SubtaskStatus.READY,
        )
        ca = Subtask(
            task_id=task.task_id,
            title="Critique A",
            description="Critique proposal A",
            required_capabilities=["review"],
            role=AgentRole.REVIEWER,
            depends_on=[a.subtask_id],
            order=1,
        )
        cb = Subtask(
            task_id=task.task_id,
            title="Critique B",
            description="Critique proposal B",
            required_capabilities=["review"],
            role=AgentRole.REVIEWER,
            depends_on=[b.subtask_id],
            order=1,
        )
        final = Subtask(
            task_id=task.task_id,
            title="Synthesize recommendation",
            description="Orchestrator synthesis — produce final recommendation from proposals and critiques",
            required_capabilities=["reasoning", "architecture"],
            role=AgentRole.FINAL_REVIEWER,
            depends_on=[ca.subtask_id, cb.subtask_id],
            order=2,
        )
        return [a, b, ca, cb, final]

    def ready_subtasks(self, task: Task) -> list[Subtask]:
        done = {s.subtask_id for s in task.subtasks if s.status == SubtaskStatus.COMPLETED}
        ready = []
        for s in task.subtasks:
            if s.status in (SubtaskStatus.PENDING, SubtaskStatus.READY, SubtaskStatus.BLOCKED):
                if all(dep in done for dep in s.depends_on):
                    s.status = SubtaskStatus.READY
                    ready.append(s)
        return ready

    def progress(self, task: Task) -> float:
        if not task.subtasks:
            return 0.0
        done = sum(1 for s in task.subtasks if s.status in (SubtaskStatus.COMPLETED, SubtaskStatus.SKIPPED))
        return done / len(task.subtasks)
