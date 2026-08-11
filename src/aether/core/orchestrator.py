"""Deterministic central orchestrator — owns task control plane."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Optional

from aether.adapters.registry import AgentRegistry
from aether.config.settings import AetherSettings
from aether.core.approvals import ApprovalManager, ApprovalStatus
from aether.core.budget import BudgetExceeded, BudgetManager
from aether.core.checkpoint import CheckpointManager
from aether.core.circuit import CircuitBreaker
from aether.core.context import ContextManager
from aether.core.context_budget import context_window_for
from aether.core.event_bus import EventBus
from aether.core.handoff import HandoffManager
from aether.core.metrics import MetricsCollector
from aether.core.quota import QuotaManager
from aether.core.router import AgentRouter
from aether.core.task_manager import InvalidStateTransition, TaskManager
from aether.core.verification import VerificationEngine
from aether.core.workflow import WorkflowEngine
from aether.core.workspace import WorkspaceManager
from aether.domain.enums import (
    AgentRole,
    ApprovalLevel,
    FailureClass,
    HandoffStatus,
    MessageType,
    SubtaskStatus,
    TaskMode,
    TaskStatus,
    VerificationStatus,
)
from aether.domain.models import (
    AgentMessage,
    ExecutionRequest,
    HandoffReport,
    Subtask,
    Task,
    TaskMemory,
)
from aether.execution_log import ExecutionLog
from aether.interfaces.adapters import AgentAdapter
from aether.observability.logging import get_logger
from aether.storage.repository import FileAuditLogger, SqliteRepository

log = get_logger("aether.orchestrator")

# Non-terminal states a handed-over runner should pick back up.
_RESUMABLE_STATUSES = {
    TaskStatus.QUEUED,
    TaskStatus.RUNNING,
    TaskStatus.PLANNING,
    TaskStatus.HANDOFF,
    TaskStatus.TESTING,
    TaskStatus.REVIEW,
}


class Orchestrator:
    """
    Central control plane. Agents never own global state.
    """

    def __init__(
        self,
        settings: AetherSettings,
        repo: SqliteRepository,
        registry: AgentRegistry,
        task_manager: TaskManager,
        router: AgentRouter,
        handoff_manager: HandoffManager,
        context_manager: ContextManager,
        checkpoint_manager: CheckpointManager,
        verification: VerificationEngine,
        workflow: WorkflowEngine,
        quota: QuotaManager,
        circuit: CircuitBreaker,
        audit: FileAuditLogger,
        event_bus: Optional[EventBus] = None,
        approvals: Optional[ApprovalManager] = None,
        budgets: Optional[BudgetManager] = None,
        metrics: Optional[MetricsCollector] = None,
        workspace_manager: Optional[WorkspaceManager] = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.registry = registry
        self.tasks = task_manager
        self.router = router
        self.handoffs = handoff_manager
        self.context = context_manager
        self.checkpoints = checkpoint_manager
        self.verification = verification
        self.workflow = workflow
        self.quota = quota
        self.circuit = circuit
        self.audit = audit
        self.events = event_bus or EventBus()
        self.approvals = approvals or ApprovalManager()
        self.budgets = budgets or BudgetManager(
            default_max_tokens=settings.budget.default_max_tokens,
            default_max_requests=settings.budget.default_max_requests,
        )
        self.metrics = metrics or MetricsCollector()
        self.workspace_mgr = workspace_manager or WorkspaceManager(
            enable_git=settings.orchestrator.enable_git_checkpoints
        )
        self._runners: dict[str, asyncio.Task[Any]] = {}
        self._stop_flags: dict[str, asyncio.Event] = {}
        self._memory_locks: dict[str, asyncio.Lock] = {}
        # subtasks this orchestrator is actively executing, per task
        self._inflight: dict[str, set[str]] = {}
        # tasks whose exiting runner must hand over to a fresh one
        self._restart_requested: set[str] = set()
        self._event_listeners: list[Callable[[dict[str, Any]], Any]] = []
        # bridge legacy listeners through event bus
        self.events.subscribe(self._legacy_listener)

    def _memory_lock(self, task_id: str) -> asyncio.Lock:
        """Serialises per-task shared state (memory, git worktree) across
        concurrently executing subtasks."""
        lock = self._memory_locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self._memory_locks[task_id] = lock
        return lock

    def _legacy_listener(self, event: dict[str, Any]) -> None:
        for listener in self._event_listeners:
            try:
                result = listener(event)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:  # noqa: BLE001
                pass

    def on_event(self, listener: Callable[[dict[str, Any]], Any]) -> None:
        self._event_listeners.append(listener)

    async def _emit(self, event: dict[str, Any]) -> None:
        event_type = event.pop("type", "event")
        await self.events.emit(event_type, **event)

    async def submit(
        self,
        prompt: str,
        mode: Optional[TaskMode] = None,
        auto_start: bool = True,
        max_tokens: Optional[int] = None,
        max_requests: Optional[int] = None,
        max_cost_usd: Optional[float] = None,
        **kwargs: Any,
    ) -> Task:
        task = await self.tasks.create_task(prompt, mode=mode, **kwargs)
        self.budgets.create(
            task.task_id,
            max_tokens=max_tokens,
            max_requests=max_requests,
            max_cost_usd=max_cost_usd,
            usd_per_1k_tokens=self.settings.budget.usd_per_1k_tokens,
        )
        if task.workspace_path:
            await self.workspace_mgr.ensure_repo(Path(task.workspace_path))
        await self.audit.log("task_submit", task_id=task.task_id, prompt=prompt[:300])
        self.metrics.record_task_event(task.task_id, "started")
        await self._emit({"type": "task_created", "task_id": task.task_id, "prompt": prompt[:200]})

        if self.settings.orchestrator.require_approval_on_start or task.approval_level == ApprovalLevel.APPROVAL:
            req = await self.approvals.request(
                task.task_id,
                operation="start_task",
                detail=f"Start task: {prompt[:200]}",
                risk="medium",
            )
            await self.tasks.timeline(task.task_id, "orchestrator", "approval_required", req.approval_id)
            await self._emit(
                {
                    "type": "approval_required",
                    "task_id": task.task_id,
                    "approval_id": req.approval_id,
                    "operation": "start_task",
                }
            )
            # leave PAUSED-like WAITING until granted via API/CLI
            task = await self.tasks.transition(task, TaskStatus.PLANNING)
            task = await self.tasks.transition(task, TaskStatus.WAITING)
            task.metadata["pending_approval_id"] = req.approval_id
            await self.tasks.save(task)
            if not auto_start:
                return task
            # if timeout configured, wait; else return waiting
            timeout = self.settings.orchestrator.approval_timeout_seconds
            if timeout and timeout > 0:
                resolved = await self.approvals.wait_for(req.approval_id, timeout_seconds=float(timeout))
                if resolved.status != ApprovalStatus.GRANTED:
                    task.failure_reason = f"Start approval {resolved.status.value}"
                    await self.tasks.save(task)
                    await self.tasks.transition(task, TaskStatus.CANCELLED)
                    return task
            else:
                return task

        if auto_start:
            await self.start_task(task.task_id)
        return task

    async def start_task(self, task_id: str) -> Task:
        task = await self.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
            return task
        if task.status == TaskStatus.PAUSED:
            task = await self.tasks.resume(task_id)
        existing = self._runners.get(task_id)
        if existing is not None and not existing.done():
            # A runner is still alive (e.g. mid-execution when the task was
            # paused). Clearing its stop flag resumes it in place — otherwise it
            # would reach its next checkpoint and pause the task straight back.
            flag = self._stop_flags.get(task_id)
            if flag is not None:
                flag.clear()
            # It may still be on its way out; ask it to hand over to a fresh
            # runner so the task is never left non-terminal with nobody driving.
            self._restart_requested.add(task_id)
            if not existing.done():
                return task
            self._restart_requested.discard(task_id)
        stop = asyncio.Event()
        self._stop_flags[task_id] = stop
        self._runners[task_id] = asyncio.create_task(self._run_task(task_id), name=f"task-{task_id}")
        return task

    async def pause_task(self, task_id: str) -> Task:
        if task_id in self._stop_flags:
            self._stop_flags[task_id].set()
        return await self.tasks.pause(task_id)

    async def cancel_task(self, task_id: str) -> Task:
        if task_id in self._stop_flags:
            self._stop_flags[task_id].set()
        runner = self._runners.get(task_id)
        if runner and not runner.done():
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass
        # kill in-flight adapter executions for every agent that touched this task
        task = await self.tasks.get(task_id)
        if task:
            providers = list(task.participating_agents)
            if task.active_agent and task.active_agent not in providers:
                providers.append(task.active_agent)
            for provider_id in providers:
                adapter = self.registry.get(provider_id)
                if adapter is None:
                    continue
                try:
                    await adapter.cancel_all()
                except Exception as exc:  # noqa: BLE001
                    log.warning("cancel_all_failed", provider=provider_id, error=str(exc))
        self.metrics.record_task_event(task_id, "cancelled")
        return await self.tasks.cancel(task_id)

    async def resolve_approval(
        self,
        approval_id: str,
        granted: bool,
        resolved_by: str = "user",
        auto_resume: bool = True,
    ) -> dict[str, Any]:
        req = self.approvals.resolve(approval_id, granted=granted, resolved_by=resolved_by)
        await self._emit(
            {
                "type": "approval_resolved",
                "task_id": req.task_id,
                "approval_id": approval_id,
                "granted": granted,
            }
        )
        await self.tasks.timeline(
            req.task_id,
            resolved_by,
            "approval_granted" if granted else "approval_denied",
            f"{req.operation}: {approval_id}",
        )
        if granted and auto_resume and req.operation in {"start_task", "resume_task", "continue"}:
            task = await self.tasks.get(req.task_id)
            if task and task.status in {TaskStatus.WAITING, TaskStatus.PAUSED}:
                await self.resume_task(req.task_id)
        return req.model_dump()

    async def resume_task(self, task_id: str) -> Task:
        task = await self.tasks.resume(task_id)
        await self.start_task(task_id)
        return task

    async def recover_running(self) -> list[str]:
        """On orchestrator restart: requeue non-terminal tasks.

        Collect every candidate *before* touching any of them. Requeuing moves a
        task into QUEUED, so scanning-and-mutating in one pass finds the same
        task again under a later status and re-saves it from a stale object —
        stomping the status its freshly started runner has already advanced to.
        """
        candidates: dict[str, Task] = {}
        for status in _RESUMABLE_STATUSES:
            for task in await self.tasks.list(status=status, limit=500):
                candidates.setdefault(task.task_id, task)

        resumed: list[str] = []
        for task_id, task in candidates.items():
            task.status = TaskStatus.QUEUED
            await self.tasks.save(task)
            await self.start_task(task_id)
            resumed.append(task_id)
            await self.tasks.timeline(
                task_id, "orchestrator", "recovered", "Resumed after orchestrator restart"
            )
        return resumed

    async def _run_task(self, task_id: str) -> None:
        cancelled = False
        try:
            await self._execute_lifecycle(task_id)
        except asyncio.CancelledError:
            cancelled = True
            log.info("task_runner_cancelled", task_id=task_id)
            raise
        except BudgetExceeded as exc:
            log.warning("task_budget_exceeded", task_id=task_id, error=str(exc))
            task = await self.tasks.get(task_id)
            if task and task.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
                task.failure_reason = f"Budget exceeded: {exc.reason}"
                task.status = TaskStatus.FAILED
                await self.tasks.save(task)
                await self.tasks.timeline(task_id, "orchestrator", "budget_exceeded", str(exc))
                self.metrics.record_task_event(task_id, "failed")
                await self._emit({"type": "task_failed", "task_id": task_id, "reason": "budget"})
        except Exception as exc:  # noqa: BLE001
            log.exception("task_runner_error", task_id=task_id, error=str(exc))
            task = await self.tasks.get(task_id)
            if task and task.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
                task.failure_reason = str(exc)
                task.status = TaskStatus.FAILED
                await self.tasks.save(task)
                await self.tasks.timeline(task_id, "orchestrator", "failed", str(exc))
                self.metrics.record_task_event(task_id, "failed")
                await self._emit({"type": "task_failed", "task_id": task_id, "reason": str(exc)[:300]})
        finally:
            # only clear our own registration — a newer runner may have replaced it
            if self._runners.get(task_id) is asyncio.current_task():
                self._runners.pop(task_id, None)
                self._stop_flags.pop(task_id, None)
            restart = task_id in self._restart_requested
            self._restart_requested.discard(task_id)
            if not cancelled and restart:
                current = await self.tasks.get(task_id)
                if current is not None and current.status in _RESUMABLE_STATUSES:
                    await self.start_task(task_id)

    def _select_planning_adapter(self) -> Optional[AgentAdapter]:
        """Pick a provider to draft the subtask DAG.

        Mirrors ``AgentRouter._has_real_provider``'s definition of "a real
        provider": the first enabled, non-testing, registered adapter.
        Planning is a single call with a hard schema gate on the result, so
        full routing (capability scoring, health probes) is unnecessary here
        — any real adapter can attempt it, and a bad answer is rejected by
        validation rather than by picking the "best" provider.
        """
        for pid in self.registry.list_ids():
            cfg = self.registry.get_config(pid)
            if cfg is not None and cfg.enabled and not cfg.for_testing:
                adapter = self.registry.get(pid)
                if adapter is not None:
                    return adapter
        return None

    async def _execute_lifecycle(self, task_id: str) -> None:
        task = await self.tasks.get(task_id)
        assert task is not None
        memory = await self.repo.get_memory(task_id)
        if memory is None:
            memory = TaskMemory(task_id=task_id, goal=task.normalized_goal)

        # PLANNING
        if task.status in (TaskStatus.CREATED, TaskStatus.QUEUED, TaskStatus.PAUSED):
            try:
                task = await self.tasks.transition(task, TaskStatus.PLANNING)
            except InvalidStateTransition:
                if task.status == TaskStatus.PAUSED:
                    task = await self.tasks.transition(task, TaskStatus.QUEUED)
                    task = await self.tasks.transition(task, TaskStatus.PLANNING)

        if not task.subtasks:
            plan_adapter = self._select_planning_adapter()
            plan_result = await self.workflow.plan_task(task, plan_adapter, self.settings)
            task.subtasks = plan_result.subtasks
            task.metadata["planner_source"] = plan_result.source
            if plan_result.source != "llm":
                # A fallback must always be discoverable, never silent — record
                # why on both the task itself and the timeline.
                task.metadata["planner_fallback_reason"] = plan_result.reason
                await self.tasks.timeline(
                    task_id, "orchestrator", "planner_fallback", plan_result.reason or "unknown"
                )
            memory.current_state = (
                f"Planned {len(task.subtasks)} subtasks (mode={task.mode.value}, planner={plan_result.source})"
            )
            memory.next_action = task.subtasks[0].title if task.subtasks else "complete"
            await self.repo.save_memory(memory)
            await self.tasks.save(task)
            await self.tasks.timeline(
                task_id,
                "orchestrator",
                "planned",
                f"{len(task.subtasks)} subtasks ({plan_result.source}): "
                + ", ".join(s.title for s in task.subtasks),
            )
            await self._msg(
                task_id,
                "orchestrator",
                "broadcast",
                MessageType.PLAN,
                f"Plan: {[s.title for s in task.subtasks]}",
            )

        task = await self.tasks.transition(task, TaskStatus.QUEUED)
        task = await self.tasks.transition(task, TaskStatus.RUNNING)
        await self._emit({"type": "task_running", "task_id": task_id})

        verification_loops = 0
        max_ver = self.settings.orchestrator.max_verification_loops

        while True:
            if self._stop_flags.get(task_id) and self._stop_flags[task_id].is_set():
                task = await self.tasks.get(task_id)
                if task and task.status != TaskStatus.CANCELLED:
                    await self.tasks.pause(task_id)
                return

            task = await self.tasks.get(task_id)
            assert task is not None
            if task.status in (TaskStatus.CANCELLED, TaskStatus.PAUSED, TaskStatus.COMPLETED):
                return
            if task.status == TaskStatus.FAILED:
                return

            memory = await self.repo.get_memory(task_id) or memory
            ready = self.workflow.ready_subtasks(task)
            unfinished = [
                s
                for s in task.subtasks
                if s.status not in (SubtaskStatus.COMPLETED, SubtaskStatus.SKIPPED, SubtaskStatus.CANCELLED)
            ]

            if not unfinished:
                # verify then complete
                task = await self.tasks.transition(task, TaskStatus.TESTING)
                result = await self.verification.verify_task(task)
                await self.tasks.timeline(
                    task_id, "verification", result.status.value, result.summary
                )
                await self._msg(
                    task_id,
                    "verification",
                    "orchestrator",
                    MessageType.TEST_RESULT,
                    result.summary,
                )
                if result.status == VerificationStatus.PASSED or result.status == VerificationStatus.SKIPPED:
                    self.metrics.record_task_event(task_id, "verification_passed")
                    task.final_output = await self._compose_final(task, memory)
                    task.progress = 1.0
                    await self.tasks.save(task)
                    await self.checkpoints.create(
                        task, memory, state_summary="Task completed", next_action="none"
                    )
                    await self.tasks.transition(task, TaskStatus.COMPLETED)
                    self.metrics.record_task_event(task_id, "completed")
                    await self._msg(
                        task_id,
                        "orchestrator",
                        "user",
                        MessageType.COMPLETION,
                        task.final_output[:2000],
                    )
                    await self._emit({"type": "task_completed", "task_id": task_id})
                    return

                self.metrics.record_task_event(task_id, "verification_failed")
                verification_loops += 1
                if verification_loops > max_ver:
                    task.failure_reason = f"Verification failed after {max_ver} loops: {result.summary}"
                    await self.tasks.save(task)
                    await self.tasks.transition(task, TaskStatus.FAILED)
                    self.metrics.record_task_event(task_id, "failed")
                    return

                # create debugger subtask
                fix = Subtask(
                    task_id=task_id,
                    title=f"Fix verification failures (loop {verification_loops})",
                    description=result.summary
                    + "\n"
                    + "\n".join(f"{c.name}: {c.detail}" for c in result.checks if c.status == VerificationStatus.FAILED),
                    required_capabilities=["coding", "testing"],
                    role=AgentRole.DEBUGGER,
                    status=SubtaskStatus.READY,
                    order=100 + verification_loops,
                )
                task.subtasks.append(fix)
                memory.known_issues.append(result.summary)
                memory.next_action = fix.title
                await self.repo.save_memory(memory)
                await self.tasks.save(task)
                await self.tasks.transition(task, TaskStatus.RUNNING)
                continue

            if not ready:
                # blocked on dependencies that failed?
                failed = [s for s in task.subtasks if s.status == SubtaskStatus.FAILED]
                if failed:
                    # try failover on failed or mark waiting
                    detail = "; ".join(s.result_summary for s in failed if s.result_summary)
                    task.failure_reason = f"Blocked; failed subtasks: {[s.title for s in failed]}" + (
                        f" — {detail[:300]}" if detail else ""
                    )
                    await self.tasks.save(task)
                    await self.tasks.transition(task, TaskStatus.FAILED)
                    return

                # A subtask left RUNNING by a runner that is no longer executing
                # it (an interrupted pause/resume, or a crashed process) is
                # invisible to both `ready` and "finished" — reclaim it instead
                # of spinning here forever.
                inflight = self._inflight.get(task_id, set())
                stranded = [
                    s.subtask_id
                    for s in task.subtasks
                    if s.status == SubtaskStatus.RUNNING and s.subtask_id not in inflight
                ]
                if stranded:
                    def _reclaim(t: Task, ids: list[str] = stranded) -> None:
                        for s in t.subtasks:
                            if s.subtask_id in ids:
                                s.status = SubtaskStatus.READY
                                s.assigned_agent = None

                    task = await self.tasks.update(task_id, _reclaim)
                    await self.tasks.timeline(
                        task_id, "orchestrator", "reclaimed", f"{len(stranded)} stranded subtask(s)"
                    )
                    continue

                if not inflight:
                    # nothing runnable, nothing running, nothing failed: the DAG
                    # can never progress. Fail loudly rather than hang.
                    blocked = [s.title for s in unfinished]
                    task.failure_reason = f"Deadlocked; no runnable subtasks remain: {blocked}"
                    await self.tasks.save(task)
                    await self.tasks.timeline(
                        task_id, "orchestrator", "deadlocked", task.failure_reason[:300]
                    )
                    await self.tasks.transition(task, TaskStatus.FAILED)
                    return

                await asyncio.sleep(0.05)
                continue

            # Execute ready subtasks — parallel when mode allows and no shared conflict risk
            concurrent = (
                min(len(ready), self.settings.orchestrator.max_concurrent_subtasks)
                if task.mode in (TaskMode.PARALLEL, TaskMode.DEBATE)
                else 1
            )
            batch = ready[:concurrent]
            if concurrent > 1 and len(batch) > 1:
                await asyncio.gather(*[self._run_subtask(task_id, s.subtask_id) for s in batch])
            else:
                await self._run_subtask(task_id, batch[0].subtask_id)

            task = await self.tasks.get(task_id)
            assert task is not None
            task.progress = self.workflow.progress(task)
            await self.tasks.save(task)
            await self._emit({"type": "task_progress", "task_id": task_id, "progress": task.progress})

    async def _run_subtask(self, task_id: str, subtask_id: str) -> None:
        self._inflight.setdefault(task_id, set()).add(subtask_id)
        try:
            await self._execute_subtask(task_id, subtask_id)
        finally:
            inflight = self._inflight.get(task_id)
            if inflight is not None:
                inflight.discard(subtask_id)
                if not inflight:
                    self._inflight.pop(task_id, None)

    async def _execute_subtask(self, task_id: str, subtask_id: str) -> None:
        task = await self.tasks.get(task_id)
        assert task is not None
        memory = await self.repo.get_memory(task_id)
        assert memory is not None
        sub = next((s for s in task.subtasks if s.subtask_id == subtask_id), None)
        if sub is None:
            return

        # A previous runner may have been killed mid-execution. Whatever the
        # agent had already produced is on disk — fold it in as a partial
        # handoff so this attempt continues rather than starting from zero.
        await self._salvage_interrupted_output(task, memory, sub)

        async def apply(mutate: Callable[[Task, Subtask], None]) -> None:
            """Persist a change to this subtask without clobbering siblings.

            In parallel/debate mode several `_run_subtask` coroutines share one
            task document, so every write is a locked read-modify-write and the
            local `task`/`sub` handles are refreshed from the merged result.
            """
            nonlocal task, sub

            def _apply(fresh: Task) -> None:
                target = next((s for s in fresh.subtasks if s.subtask_id == subtask_id), None)
                if target is not None:
                    mutate(fresh, target)

            task = await self.tasks.update(task_id, _apply)
            sub = next((s for s in task.subtasks if s.subtask_id == subtask_id), sub)

        await apply(lambda t, s: setattr(s, "status", SubtaskStatus.RUNNING))

        exclude: list[str] = []
        attempts = 0
        max_retries = self.settings.orchestrator.max_agent_retries
        last_error = ""

        while attempts <= max_retries:
            if self._stop_flags.get(task_id) and self._stop_flags[task_id].is_set():
                await apply(lambda t, s: setattr(s, "status", SubtaskStatus.BLOCKED))
                return

            decision = await self.router.select(task, subtask=sub, role=sub.role, exclude=exclude)
            if decision is None:
                def _no_agents(t: Task, s: Subtask) -> None:
                    s.status = SubtaskStatus.FAILED
                    s.result_summary = "No available agents"
                    t.failure_reason = "No available agents for routing"

                await apply(_no_agents)
                await self.tasks.timeline(task_id, "orchestrator", "routing_failed", sub.title)
                return

            provider_id = decision.provider_id
            adapter = self.registry.get(provider_id)
            if adapter is None:
                exclude.append(provider_id)
                attempts += 1
                continue

            def _assign(t: Task, s: Subtask, provider_id: str = provider_id) -> None:
                t.active_agent = provider_id
                if provider_id not in t.participating_agents:
                    t.participating_agents.append(provider_id)
                s.assigned_agent = provider_id

            await apply(_assign)
            # Record *why*, not just what. The router already scores every
            # candidate and keeps its rationale, and all of it used to be
            # discarded here — leaving "assigned to X" with no way to answer
            # "why not Y?" after the fact. An orchestrator whose whole claim is
            # that it, not the model, decides should be able to show its work.
            rationale = ", ".join(decision.reasons[:6]) if decision.reasons else "no factors recorded"
            detail = (
                f"{sub.title} → {provider_id} (role={sub.role.value}) "
                f"score={decision.score:.2f} [{rationale}]"
            )
            if decision.alternatives:
                detail += f" over {', '.join(decision.alternatives[:3])}"
            await self.tasks.timeline(task_id, "orchestrator", "assigned", detail)
            await self._emit(
                {
                    "type": "agent_assigned",
                    "task_id": task_id,
                    "provider": provider_id,
                    "subtask": sub.title,
                    "score": round(decision.score, 4),
                    "reasons": decision.reasons,
                    "alternatives": decision.alternatives,
                }
            )

            # budget gate
            try:
                self.budgets.check(task_id)
            except BudgetExceeded as exc:
                def _budget(t: Task, s: Subtask, exc: BudgetExceeded = exc) -> None:
                    s.status = SubtaskStatus.FAILED
                    s.result_summary = str(exc)
                    t.failure_reason = str(exc)

                await apply(_budget)
                raise

            latest_cp = await self.checkpoints.latest(task_id)
            # Budget the package against the provider actually about to receive
            # it: windows differ by an order of magnitude between providers, so
            # "fits" is only meaningful per-provider.
            provider_cfg = self.registry.get_config(provider_id)
            window = context_window_for(
                provider_id,
                config_window=provider_cfg.context_window if provider_cfg else None,
            )
            pkg = await self.context.build_package(
                task,
                memory,
                assignment=f"{sub.title}: {sub.description}",
                role=sub.role,
                subtask=sub,
                checkpoint_id=latest_cp.checkpoint_id if latest_cp else None,
                context_window=window,
            )
            if pkg.compacted:
                await self.tasks.timeline(
                    task_id,
                    "orchestrator",
                    "context_compacted",
                    f"trimmed to fit {provider_id}'s {window}-token window",
                )
            prompt = pkg.to_prompt_block() + "\n\n" + sub.description

            await self._msg(
                task_id,
                "orchestrator",
                provider_id,
                MessageType.TASK,
                f"Assignment: {sub.title}",
            )

            # Optional per-agent worktree. Each agent gets its own checkout of
            # the same repo, so concurrent subtasks edit real files at the same
            # time instead of fighting over one shared working tree.
            agent_branch = None
            agent_dir = task.workspace_path
            if (
                self.settings.orchestrator.enable_agent_branches
                and task.workspace_path
                and task.mode in (TaskMode.PARALLEL, TaskMode.COLLAB, TaskMode.DEBATE)
            ):
                agent_branch, worktree = await self.workspace_mgr.create_agent_worktree(
                    Path(task.workspace_path), provider_id, sub.subtask_id
                )
                agent_dir = str(worktree)

            req = ExecutionRequest(
                task_id=task_id,
                subtask_id=sub.subtask_id,
                provider_id=provider_id,
                role=sub.role,
                prompt=prompt,
                context_package=pkg.model_dump(mode="json"),
                workspace_path=agent_dir,
                timeout_seconds=self.settings.orchestrator.agent_timeout_seconds,
            )

            # pre-check quota
            avail = self.quota.availability(provider_id)
            from aether.domain.enums import ProviderAvailability

            if avail != ProviderAvailability.AVAILABLE:
                exclude.append(provider_id)
                attempts += 1
                last_error = f"quota/availability: {avail.value}"
                continue

            await self._emit(
                {
                    "type": "agent_started",
                    "task_id": task_id,
                    "provider": provider_id,
                    "subtask": sub.title,
                    "execution_id": req.execution_id,
                }
            )

            # Tell the router this provider is busy for the duration, so a
            # sibling subtask selecting concurrently sees the load and can pick
            # someone else. try/finally, because a provider left counted as busy
            # after a crash would be quietly deprioritised forever.
            self.router.note_dispatch(provider_id)
            try:
                result = await adapter.execute(req)
                # broadcast output for live UI (no second provider call)
                if result.output:
                    await self._emit(
                        {
                            "type": "agent_stream",
                            "task_id": task_id,
                            "provider": provider_id,
                            "execution_id": req.execution_id,
                            "text": result.output[:4000],
                            "done": True,
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                result = None
                last_error = str(exc)
                self.circuit.record_failure(provider_id, FailureClass.CRASH)
                self.quota.record_failure(provider_id)
                self.metrics.record_agent_execution(provider_id, 0.0, False, task_id=task_id)
                await self._failover_prep(task, memory, provider_id, str(exc), FailureClass.CRASH)
                exclude.append(provider_id)
                attempts += 1
                continue
            finally:
                # Runs on the `continue` too. Without this, a provider that
                # crashed would stay counted as busy and be deprioritised for
                # the lifetime of the process.
                self.router.note_complete(provider_id)

            tokens = len(result.output or "") // 4
            self.quota.record_request(provider_id, tokens=tokens)
            try:
                self.budgets.record(task_id, tokens=tokens, requests=1)
            except BudgetExceeded:
                # allow this result to be processed then fail task
                pass
            self.metrics.record_agent_execution(
                provider_id, result.latency_ms, result.success, task_id=task_id
            )

            if not result.success:
                fc = result.failure_class or FailureClass.UNKNOWN
                last_error = result.error or "execution failed"

                if fc == FailureClass.POLICY_DENIED:
                    # An operator policy decision, not a provider fault: don't
                    # penalise the provider, and don't retry the same denied
                    # command on a different one.
                    def _denied(t: Task, s: Subtask, reason: str = last_error) -> None:
                        s.status = SubtaskStatus.FAILED
                        s.result_summary = reason
                        t.failure_reason = reason
                        t.active_agent = None

                    await apply(_denied)
                    await self.tasks.timeline(
                        task_id, provider_id, "policy_denied", last_error[:300]
                    )
                    await self._msg(
                        task_id, provider_id, "orchestrator", MessageType.ERROR, last_error[:500]
                    )
                    await self._emit(
                        {
                            "type": "policy_denied",
                            "task_id": task_id,
                            "provider": provider_id,
                            "detail": last_error[:300],
                        }
                    )
                    return

                self.circuit.record_failure(provider_id, fc)
                self.quota.record_failure(provider_id)
                if fc == FailureClass.RATE_LIMIT:
                    self.quota.set_cooldown(provider_id, 60)
                if fc == FailureClass.QUOTA:
                    self.quota.mark_quota_exhausted(provider_id)

                async with self._memory_lock(task_id):
                    memory = await self.repo.get_memory(task_id) or memory
                    handoff = result.handoff or self.handoffs.synthesize(
                        task_id,
                        provider_id,
                        sub.description,
                        output=result.output or "",
                        error=last_error,
                        memory=memory,
                    )
                    await self.handoffs.accept(handoff)
                    memory = await self.handoffs.apply_to_memory(memory, handoff)
                    await self.checkpoints.create(
                        task,
                        memory,
                        agent_id=provider_id,
                        state_summary=f"Failure checkpoint: {fc.value}",
                        next_action=handoff.recommended_next_action,
                    )
                await self.tasks.timeline(task_id, provider_id, "error", f"{fc.value}: {last_error[:300]}")
                await self._msg(task_id, provider_id, "orchestrator", MessageType.ERROR, last_error[:500])

                # Auth failures shouldn't hammer same provider
                exclude.append(provider_id)
                attempts += 1
                await apply(lambda t, s: setattr(s, "retries", s.retries + 1))
                self.metrics.record_task_event(task_id, "failover", from_provider=provider_id)
                await self.tasks.timeline(
                    task_id,
                    "orchestrator",
                    "failover",
                    f"{provider_id} failed ({fc.value}); selecting next agent",
                )
                await self._emit(
                    {
                        "type": "failover",
                        "task_id": task_id,
                        "from": provider_id,
                        "failure": fc.value,
                    }
                )
                continue

            # Success path. Everything below mutates shared per-task state
            # (memory, the git worktree, the task document) so it is serialised
            # even when sibling subtasks are executing in parallel.
            self.circuit.record_success(provider_id)
            async with self._memory_lock(task_id):
                memory = await self.repo.get_memory(task_id) or memory
                handoff = result.handoff
                if handoff is None:
                    handoff = self.handoffs.synthesize(
                        task_id, provider_id, sub.description, output=result.output or "", memory=memory
                    )
                handoff.to_agent = "orchestrator"
                await self.handoffs.accept(handoff)
                # the output is now durable inside the handoff; the raw capture
                # would otherwise be salvaged a second time on the next attempt
                if task.workspace_path:
                    ExecutionLog(Path(task.workspace_path), sub.subtask_id).clear()
                memory = await self.handoffs.apply_to_memory(memory, handoff)
                self.metrics.record_task_event(task_id, "handoff", provider=provider_id)

                # Commit inside the agent's own worktree, then merge from the
                # primary tree. The merge stays inside this lock because it
                # mutates the one shared checkout — only the editing above is
                # genuinely concurrent.
                if agent_branch and task.workspace_path:
                    await self.workspace_mgr.commit_agent_work(
                        Path(agent_dir), provider_id, handoff.recommended_next_action or sub.title
                    )
                    ok, conflicts, detail = await self.workspace_mgr.merge_to_main(
                        Path(task.workspace_path), agent_branch
                    )
                    if not ok:
                        memory.known_issues.append(f"merge_conflict: {', '.join(conflicts)}")
                        await self.repo.save_memory(memory)
                        # the worktree stays put: a human resolving the conflict
                        # needs the agent's version still on disk
                        await self.tasks.timeline(
                            task_id, "orchestrator", "merge_conflict", ", ".join(conflicts) or detail[:200]
                        )
                        await self._emit(
                            {
                                "type": "merge_conflict",
                                "task_id": task_id,
                                "files": conflicts,
                                "branch": agent_branch,
                            }
                        )
                        # spawn conflict resolution subtask rather than silent overwrite
                        conflict_sub = Subtask(
                            task_id=task_id,
                            title="Resolve merge conflicts",
                            description=f"Resolve conflicts in: {', '.join(conflicts)}\n{detail[:500]}",
                            required_capabilities=["coding"],
                            role=AgentRole.DEBUGGER,
                            status=SubtaskStatus.READY,
                            order=90,
                        )
                        await apply(lambda t, s, cs=conflict_sub: t.subtasks.append(cs))
                    else:
                        # merged cleanly: the checkout has served its purpose
                        await self.workspace_mgr.remove_agent_worktree(
                            Path(task.workspace_path), agent_branch
                        )

                # index artifacts
                for art in result.artifacts:
                    if art not in memory.files_index:
                        memory.files_index.append(art)
                await self._index_workspace_files(memory, Path(task.workspace_path))

                cp = await self.checkpoints.create(
                    task,
                    memory,
                    agent_id=provider_id,
                    state_summary=f"{sub.title}: {handoff.status.value}",
                    tests_summary=handoff.test_results,
                    next_action=handoff.recommended_next_action,
                )

            await self.tasks.timeline(
                task_id,
                provider_id,
                "handoff",
                f"{handoff.status.value}: completed={len(handoff.completed)} remaining={len(handoff.remaining)}",
            )
            await self._msg(
                task_id,
                provider_id,
                "orchestrator",
                MessageType.HANDOFF,
                handoff.recommended_next_action or handoff.status.value,
            )
            await self._emit(
                {
                    "type": "agent_finished",
                    "task_id": task_id,
                    "provider": provider_id,
                    "success": True,
                    "checkpoint_id": cp.checkpoint_id,
                }
            )

            if handoff.status in (HandoffStatus.FAILED, HandoffStatus.BLOCKED):
                exclude.append(provider_id)
                attempts += 1
                last_error = "; ".join(handoff.known_bugs) or "blocked/failed handoff"
                continue

            # Mark complete for COMPLETE or PARTIAL with no critical remaining for this subtask
            if handoff.status == HandoffStatus.COMPLETE or (
                handoff.status == HandoffStatus.PARTIAL and attempts >= max_retries
            ):
                def _complete(t: Task, s: Subtask, ho: HandoffReport = handoff) -> None:
                    s.status = SubtaskStatus.COMPLETED
                    s.result_summary = "; ".join(ho.completed) or ho.status.value
                    s.artifacts = list(ho.files_changed)
                    t.active_agent = None

                await apply(_complete)
                await self.repo.save_memory(memory)
                await self.tasks.timeline(
                    task_id, "orchestrator", "subtask_complete", f"{sub.title} via {provider_id}"
                )
                return

            # PARTIAL with remaining — if same agent can continue, retry same; else failover
            if handoff.status == HandoffStatus.PARTIAL:
                # allow same agent one more go, then failover
                attempts += 1
                if attempts > max_retries:
                    # accept partial as done for this subtask to avoid infinite loop
                    def _partial(t: Task, s: Subtask, ho: HandoffReport = handoff) -> None:
                        s.status = SubtaskStatus.COMPLETED
                        s.result_summary = f"PARTIAL accepted after retries: {ho.recommended_next_action}"
                        t.active_agent = None

                    await apply(_partial)
                    await self.repo.save_memory(memory)
                    return
                # continue loop (may reselect same)
                continue

        # exhausted retries
        def _exhausted(t: Task, s: Subtask, reason: str = last_error) -> None:
            s.status = SubtaskStatus.FAILED
            s.result_summary = reason or "max retries exceeded"
            t.active_agent = None

        await apply(_exhausted)
        await self.tasks.timeline(task_id, "orchestrator", "subtask_failed", f"{sub.title}: {last_error}")

    async def _salvage_interrupted_output(
        self, task: Task, memory: TaskMemory, sub: Subtask
    ) -> bool:
        """Fold a killed execution's partial output back into the task.

        Adapters mirror agent output to disk as it arrives, so a process that
        died mid-execution still left everything it had received. Turning that
        into a handoff is the difference between resuming and starting over.
        """
        if not task.workspace_path:
            return False
        partial = ExecutionLog(Path(task.workspace_path), sub.subtask_id)
        if not partial.exists():
            return False

        text = partial.read()
        if not text.strip():
            partial.clear()
            return False

        agent = sub.assigned_agent or "unknown-agent"
        handoff = self.handoffs.synthesize(
            task.task_id, agent, sub.description, output=text
        )
        handoff.assumptions = list(handoff.assumptions) + [
            "Salvaged from an execution that was interrupted before it returned"
        ]
        await self.handoffs.accept(handoff)
        await self.handoffs.apply_to_memory(memory, handoff)
        await self.tasks.timeline(
            task.task_id,
            agent,
            "salvaged",
            f"{len(text)} chars recovered from an interrupted run of '{sub.title}'",
        )
        await self._emit(
            {
                "type": "output_salvaged",
                "task_id": task.task_id,
                "subtask": sub.title,
                "characters": len(text),
            }
        )
        partial.clear()
        log.info(
            "salvaged_partial_output",
            task_id=task.task_id,
            subtask=sub.subtask_id,
            characters=len(text),
        )
        return True

    async def _failover_prep(
        self,
        task: Task,
        memory: TaskMemory,
        provider_id: str,
        error: str,
        fc: FailureClass,
    ) -> None:
        handoff = self.handoffs.synthesize(
            task.task_id, provider_id, memory.next_action or task.original_prompt, error=error, memory=memory
        )
        await self.handoffs.accept(handoff)
        await self.handoffs.apply_to_memory(memory, handoff)
        await self.checkpoints.create(
            task, memory, agent_id=provider_id, state_summary=f"Failover: {fc.value}", next_action=memory.next_action
        )

    async def _index_workspace_files(self, memory: TaskMemory, workspace: Path) -> None:
        if not workspace.exists():
            return
        for p in workspace.rglob("*"):
            if p.is_file() and ".git" not in p.parts:
                rel = str(p.relative_to(workspace))
                if rel not in memory.files_index:
                    memory.files_index.append(rel)
        memory.files_index = memory.files_index[-200:]
        await self.repo.save_memory(memory)

    async def _compose_final(self, task: Task, memory: TaskMemory) -> str:
        lines = [
            f"# AETHER Task Result: {task.task_id}",
            "",
            f"**Goal:** {task.normalized_goal}",
            "**Status:** COMPLETED",
            f"**Mode:** {task.mode.value}",
            f"**Agents:** {', '.join(task.participating_agents) or 'none'}",
            "",
            "## Final State",
            memory.current_state,
            "",
            "## Decisions",
        ]
        for d in memory.decisions[-20:]:
            lines.append(f"- ({d.made_by}) {d.text}")
        lines.extend(["", "## Files", *[f"- {f}" for f in memory.files_index[:50] or ["(none)"]]])
        lines.extend(["", "## Known Issues", *[f"- {i}" for i in memory.known_issues[-10:] or ["none"]]])
        handoffs = await self.repo.list_handoffs(task.task_id)
        if handoffs:
            lines.extend(["", "## Handoff Trail"])
            for h in handoffs[-10:]:
                lines.append(f"- {h.from_agent}: {h.status.value} — {h.recommended_next_action[:120]}")
        # write to workspace
        out = "\n".join(lines)
        try:
            path = Path(task.workspace_path) / "artifacts" / "FINAL_RESULT.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(out, encoding="utf-8")
        except OSError:
            pass
        return out

    async def _msg(
        self,
        task_id: str,
        sender: str,
        recipient: str,
        mtype: MessageType,
        content: str,
    ) -> None:
        msg = AgentMessage(
            task_id=task_id,
            sender=sender,
            recipient=recipient,
            message_type=mtype,
            content=content,
        )
        await self.repo.append_message(msg)

    async def inspect(self, task_id: str) -> dict[str, Any]:
        task = await self.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        memory = await self.repo.get_memory(task_id)
        events = await self.repo.list_events(task_id)
        handoffs = await self.repo.list_handoffs(task_id)
        checkpoints = await self.repo.list_checkpoints(task_id)
        messages = await self.repo.list_messages(task_id)
        verifications = await self.repo.list_results(task_id)
        return {
            "task": task.model_dump(mode="json"),
            "memory": memory.model_dump(mode="json") if memory else None,
            "timeline": [e.model_dump(mode="json") for e in events],
            "handoffs": [h.model_dump(mode="json") for h in handoffs],
            "checkpoints": [c.model_dump(mode="json") for c in checkpoints],
            "messages": [m.model_dump(mode="json") for m in messages],
            "verifications": [v.model_dump(mode="json") for v in verifications],
            "budget": self.budgets.snapshot(task_id),
            "trace": self.metrics.task_trace(task_id),
            "live_events": self.events.history(task_id=task_id, limit=50),
            "pending_approvals": [
                a.model_dump() for a in self.approvals.list_pending(task_id=task_id)
            ],
        }
