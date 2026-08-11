"""LLM planner — asks a model for a subtask DAG, validates it hard before it is
trusted, and never touches execution.

WO-11: the keyword planner (`WorkflowEngine._plan_solo` et al.) picks a task's
shape from a fixed keyword list, so anything that doesn't say "build" or
"research" falls into the generic two-step path regardless of how much
structure the request actually has. This module asks a model for a richer
subtask DAG instead — but the orchestrator still owns *execution*: the model
only ever proposes titles, descriptions, roles and dependencies, all of which
are validated against a hard schema before a single subtask is trusted. Any
validation failure — bad JSON, an unknown role, a cycle, too many subtasks —
falls back to the deterministic keyword planner and records *why*, so the
fallback is never silent.

Budget: at most one model call per task, wrapped in a timeout, no retries and
no loop. A planner that hangs or waffles is worse than the keyword fallback.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from aether.config.settings import AetherSettings
from aether.domain.enums import AgentRole, SubtaskStatus
from aether.domain.models import ExecutionRequest, Subtask, Task
from aether.interfaces.adapters import AgentAdapter
from aether.observability.logging import get_logger

log = get_logger("aether.planner")

# One model call, hard-capped, independent of the general agent execution
# timeout — planning that hangs must not stall the whole task.
PLANNER_TIMEOUT_SECONDS = 90

_ROLE_VALUES = {r.value for r in AgentRole}

KeywordFallback = Callable[[Task], list[Subtask]]


@dataclass
class PlanResult:
    """The outcome of a planning attempt.

    ``source`` is ``"llm"`` when the model's plan passed validation, or
    ``"keyword"`` when the deterministic fallback was used — in which case
    ``reason`` explains why, so a fallback is always discoverable and never
    silent.
    """

    subtasks: list[Subtask]
    source: str
    reason: str = ""


class LLMPlanner:
    """Turns a free-text task prompt into a validated subtask DAG."""

    def __init__(self, settings: AetherSettings) -> None:
        self.settings = settings

    async def plan_task(
        self,
        task: Task,
        adapter: Optional[AgentAdapter],
        keyword_fallback: KeywordFallback,
    ) -> PlanResult:
        """Plan ``task`` via the model behind ``adapter``.

        Falls back to ``keyword_fallback`` (the deterministic planner) when
        the LLM planner is disabled, no adapter is available, the call times
        out or errors, or the returned plan fails validation.
        """
        if not self.settings.orchestrator.use_llm_planner:
            return PlanResult(keyword_fallback(task), source="keyword", reason="use_llm_planner disabled")

        if adapter is None:
            return PlanResult(
                keyword_fallback(task), source="keyword", reason="no provider available for planning"
            )

        try:
            raw = await asyncio.wait_for(self._ask_model(task, adapter), timeout=PLANNER_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return PlanResult(
                keyword_fallback(task),
                source="keyword",
                reason=f"planner timed out after {PLANNER_TIMEOUT_SECONDS}s",
            )
        except Exception as exc:  # noqa: BLE001 — any adapter failure falls back, never raises
            return PlanResult(keyword_fallback(task), source="keyword", reason=f"planner call failed: {exc}")

        subtasks, error = self._validate(task, raw)
        if error:
            log.info("planner_fallback", task_id=task.task_id, reason=error)
            return PlanResult(keyword_fallback(task), source="keyword", reason=error)
        return PlanResult(subtasks, source="llm")

    async def _ask_model(self, task: Task, adapter: AgentAdapter) -> str:
        req = ExecutionRequest(
            task_id=task.task_id,
            provider_id=adapter.provider_id,
            role=AgentRole.ARCHITECT,
            prompt=self._build_prompt(task),
            workspace_path=task.workspace_path,
            timeout_seconds=PLANNER_TIMEOUT_SECONDS,
        )
        result = await adapter.execute(req)
        if not result.success:
            raise RuntimeError(result.error or "planner execution failed")
        return result.output or ""

    def _build_prompt(self, task: Task) -> str:
        roles = ", ".join(sorted(_ROLE_VALUES))
        max_n = self.settings.orchestrator.planner_max_subtasks
        return (
            "You are a task planner for an autonomous engineering/research orchestrator. "
            "Break the task below into a small DAG of subtasks that a set of specialised "
            "agents will execute. Respond with JSON ONLY: a list of objects, no prose, no "
            "markdown fences, no trailing commentary.\n\n"
            "Each object must have:\n"
            '  "title": short, unique, non-empty string\n'
            '  "description": what this subtask must accomplish, specific to the task\n'
            f'  "role": exactly one of [{roles}]\n'
            '  "required_capabilities": list of capability strings (e.g. "coding", "testing", '
            '"architecture", "research", "review")\n'
            '  "depends_on": list of the titles (or 0-based indices) of subtasks in this same '
            "list that must complete first — omit or use [] for a subtask with no prerequisites\n\n"
            f"Rules: emit between 1 and {max_n} subtasks. At least one subtask must have no "
            "dependencies. Do not invent roles outside the list above. Prefer a plan with real "
            "dependency structure over a flat list when the task has natural stages (e.g. "
            "design before implementation, implementation before review/tests).\n\n"
            f"TASK: {task.original_prompt}\n"
        )

    def _validate(self, task: Task, raw: str) -> tuple[list[Subtask], str]:
        data = self._extract_json(raw)
        if data is None:
            return [], "could not parse JSON from planner output"

        if isinstance(data, dict):
            for key in ("subtasks", "plan", "tasks"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break

        if not isinstance(data, list):
            return [], "planner output is not a JSON list of subtasks"
        if not data:
            return [], "planner returned zero subtasks"

        max_n = self.settings.orchestrator.planner_max_subtasks
        if len(data) > max_n:
            return [], f"planner returned {len(data)} subtasks, exceeding max {max_n}"

        items: list[dict[str, Any]] = []
        for i, raw_item in enumerate(data):
            if not isinstance(raw_item, dict):
                return [], f"subtask {i} is not a JSON object"
            title = str(raw_item.get("title") or "").strip()
            if not title:
                return [], f"subtask {i} has an empty title"
            role_raw = str(raw_item.get("role") or "").strip().lower()
            if role_raw not in _ROLE_VALUES:
                return [], f"subtask {i} ('{title}') has unknown role {role_raw!r}"
            items.append(raw_item)

        titles = [str(it.get("title")).strip() for it in items]
        title_to_index: dict[str, int] = {}
        for idx, t in enumerate(titles):
            title_to_index.setdefault(t, idx)

        subtasks: list[Subtask] = []
        for it in items:
            caps = [str(c).strip() for c in (it.get("required_capabilities") or []) if str(c).strip()]
            subtasks.append(
                Subtask(
                    task_id=task.task_id,
                    title=str(it.get("title")).strip(),
                    description=str(it.get("description") or "").strip(),
                    role=AgentRole(str(it.get("role")).strip().lower()),
                    required_capabilities=caps or ["reasoning"],
                    status=SubtaskStatus.PENDING,
                    order=0,
                )
            )

        # Resolve depends_on -> real subtask_id, validating every reference.
        for i, (it, sub) in enumerate(zip(items, subtasks)):
            deps_raw = it.get("depends_on") or []
            if not isinstance(deps_raw, list):
                return [], f"subtask {i} ('{sub.title}') depends_on is not a list"
            resolved: list[str] = []
            for dep in deps_raw:
                dep_index = self._resolve_dep_index(dep, title_to_index, len(subtasks))
                if dep_index is None:
                    return [], f"subtask {i} ('{sub.title}') has an unresolved dependency {dep!r}"
                if dep_index == i:
                    return [], f"subtask {i} ('{sub.title}') depends on itself"
                resolved.append(subtasks[dep_index].subtask_id)
            sub.depends_on = resolved

        depths = self._topo_order(subtasks)
        if depths is None:
            return [], "planner produced a cyclic dependency graph"
        for sub, depth in zip(subtasks, depths):
            sub.order = depth

        if not any(not s.depends_on for s in subtasks):
            return [], "planner produced a DAG with no root subtask (every subtask has a dependency)"

        return subtasks, ""

    @staticmethod
    def _resolve_dep_index(
        dep: Any, title_to_index: dict[str, int], n: int
    ) -> Optional[int]:
        if isinstance(dep, bool):
            return None
        if isinstance(dep, int):
            idx = dep
        elif isinstance(dep, str) and dep.strip().isdigit():
            idx = int(dep.strip())
        elif isinstance(dep, str) and dep.strip() in title_to_index:
            idx = title_to_index[dep.strip()]
        else:
            return None
        if 0 <= idx < n:
            return idx
        return None

    @staticmethod
    def _extract_json(raw: str) -> Any:
        text = (raw or "").strip()
        if not text:
            return None
        fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Last resort: the first top-level [...] or {...} slice in the text.
        for open_ch, close_ch in (("[", "]"), ("{", "}")):
            start = text.find(open_ch)
            end = text.rfind(close_ch)
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    continue
        return None

    @staticmethod
    def _topo_order(subtasks: list[Subtask]) -> Optional[list[int]]:
        """Per-node dependency depth via DFS, or None if the graph is cyclic."""
        id_to_index = {s.subtask_id: i for i, s in enumerate(subtasks)}
        n = len(subtasks)
        depth = [-1] * n
        visiting = [False] * n

        def visit(i: int) -> bool:
            if depth[i] != -1:
                return True
            if visiting[i]:
                return False
            visiting[i] = True
            d = 0
            for dep_id in subtasks[i].depends_on:
                dep_i = id_to_index.get(dep_id)
                if dep_i is None:
                    continue
                if not visit(dep_i):
                    return False
                d = max(d, depth[dep_i] + 1)
            visiting[i] = False
            depth[i] = d
            return True

        for i in range(n):
            if not visit(i):
                return None
        return depth
