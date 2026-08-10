"""Context manager — builds scoped context packages for agents.

Packages are budgeted against the *target provider's* context window. Without
that, a long task quietly grows its decision log and file index past the window,
the provider rejects the request as CONTEXT, and the retry sends the same
oversized package — so the task fails repeatedly for a reason that looks like a
provider problem and is not.

What gets dropped is ordered by how much a fresh agent needs it. The assignment,
the goal and the latest handoff's next action are never dropped: an agent that
loses those is not resuming the task, it is guessing at it.
"""

from __future__ import annotations

from typing import Optional

from aether.core.context_budget import ContextBudget, estimate_tokens
from aether.domain.enums import AgentRole
from aether.domain.models import ContextPackage, HandoffReport, Subtask, Task, TaskMemory
from aether.storage.repository import SqliteRepository

# Full-size slices, used when the package already fits.
MAX_DECISIONS = 20
MAX_FILES = 100
MAX_ISSUES = 20

# Progressively tighter (decisions, files, issues) budgets. Files go first:
# a file index is a hint an agent can rediscover by looking, whereas a decision
# is a judgement it cannot re-derive and would otherwise contradict.
_COMPACTION_STEPS: list[tuple[int, int, int]] = [
    (MAX_DECISIONS, MAX_FILES, MAX_ISSUES),
    (20, 40, 15),
    (15, 20, 10),
    (10, 10, 8),
    (8, 5, 5),
    (5, 0, 3),
    (3, 0, 2),
    (1, 0, 1),
    (0, 0, 0),
]


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
        context_window: Optional[int] = None,
    ) -> ContextPackage:
        """Assemble the package, trimmed to fit `context_window` when given."""
        if handoff is None:
            handoff = await self.repo.latest_handoff(task.task_id)

        default_constraints = constraints or [
            "Work only within the assigned workspace.",
            "Do not invent secrets or credentials.",
            "Produce a structured HANDOFF REPORT when finished.",
            f"Approval level: {task.approval_level.value}",
        ]

        def build(decisions: int, files: int, issues: int) -> ContextPackage:
            return ContextPackage(
                task_id=task.task_id,
                goal=memory.goal or task.normalized_goal or task.original_prompt,
                current_state=memory.current_state,
                decisions=[d.text for d in memory.decisions[-decisions:]] if decisions else [],
                files=list(memory.files_index[-files:]) if files else [],
                known_issues=list(memory.known_issues[-issues:]) if issues else [],
                last_handoff=handoff,
                checkpoint_id=checkpoint_id,
                your_assignment=assignment,
                role=role,
                constraints=default_constraints,
                subtask=subtask,
                summary=memory.summary,
            )

        pkg = build(*_COMPACTION_STEPS[0])
        if not context_window:
            return pkg

        limit = ContextBudget(context_window=context_window).soft_limit
        if estimate_tokens(pkg.to_prompt_block()) <= limit:
            return pkg

        # Shed the cheapest-to-lose context first.
        for decisions, files, issues in _COMPACTION_STEPS[1:]:
            pkg = build(decisions, files, issues)
            pkg.compacted = True
            if estimate_tokens(pkg.to_prompt_block()) <= limit:
                return pkg

        # Still over: the handoff itself is the bulk. Keep only the one field a
        # resuming agent cannot work without.
        if pkg.last_handoff is not None:
            pkg.last_handoff = pkg.last_handoff.model_copy(
                update={
                    "completed": [],
                    "remaining": pkg.last_handoff.remaining[:3],
                    "files_changed": [],
                    "decisions": [],
                    "tests_executed": [],
                    "logs": [],
                    "assumptions": [],
                    "known_bugs": pkg.last_handoff.known_bugs[:2],
                }
            )
            if estimate_tokens(pkg.to_prompt_block()) <= limit:
                return pkg

        # A summary this large is itself the problem; truncate rather than
        # hand the provider something it will certainly reject.
        if pkg.summary:
            pkg.summary = pkg.summary[: max(0, limit * 2)]
        return pkg
