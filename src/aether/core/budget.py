"""Per-task token/cost budget tracking."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from aether.core.state_store import JsonStateStore
from aether.domain.models import utcnow


class BudgetExceeded(Exception):
    def __init__(self, task_id: str, reason: str) -> None:
        self.task_id = task_id
        self.reason = reason
        super().__init__(reason)


class TaskBudget(BaseModel):
    task_id: str
    max_tokens: Optional[int] = None
    max_requests: Optional[int] = None
    max_cost_usd: Optional[float] = None
    used_tokens: int = 0
    used_requests: int = 0
    estimated_cost_usd: float = 0.0
    # rough default rates (configurable per provider later)
    usd_per_1k_tokens: float = 0.002
    updated_at: str = Field(default_factory=lambda: utcnow().isoformat())

    @property
    def tokens_remaining(self) -> Optional[int]:
        if self.max_tokens is None:
            return None
        return max(0, self.max_tokens - self.used_tokens)

    @property
    def exhausted(self) -> bool:
        if self.max_tokens is not None and self.used_tokens >= self.max_tokens:
            return True
        if self.max_requests is not None and self.used_requests >= self.max_requests:
            return True
        if self.max_cost_usd is not None and self.estimated_cost_usd >= self.max_cost_usd:
            return True
        return False

    def usage_pct(self) -> float:
        parts: list[float] = []
        if self.max_tokens:
            parts.append(self.used_tokens / self.max_tokens)
        if self.max_requests:
            parts.append(self.used_requests / self.max_requests)
        if self.max_cost_usd:
            parts.append(self.estimated_cost_usd / self.max_cost_usd)
        return max(parts) if parts else 0.0


class BudgetManager:
    def __init__(
        self,
        default_max_tokens: Optional[int] = None,
        default_max_requests: Optional[int] = None,
        store: Optional[JsonStateStore] = None,
    ) -> None:
        self.default_max_tokens = default_max_tokens
        self.default_max_requests = default_max_requests
        self.store = store
        self._budgets: dict[str, TaskBudget] = {}
        self._reload()


    def _transaction(self):
        """Exclusive lock across reload+mutate+flush; no-op without a store."""
        if self.store is None:
            from contextlib import nullcontext

            return nullcontext()
        return self.store.transaction()

    def _reload(self) -> None:
        if self.store is None:
            return
        self._budgets = {
            task_id: TaskBudget.model_validate(item)
            for task_id, item in (self.store.load().get("budgets") or {}).items()
        }

    def _flush(self) -> None:
        if self.store is None:
            return
        self.store.save(
            {"budgets": {tid: b.model_dump(mode="json") for tid, b in self._budgets.items()}}
        )

    def create(
        self,
        task_id: str,
        max_tokens: Optional[int] = None,
        max_requests: Optional[int] = None,
        max_cost_usd: Optional[float] = None,
        usd_per_1k_tokens: float = 0.002,
    ) -> TaskBudget:
        budget = TaskBudget(
            task_id=task_id,
            max_tokens=max_tokens if max_tokens is not None else self.default_max_tokens,
            max_requests=max_requests if max_requests is not None else self.default_max_requests,
            max_cost_usd=max_cost_usd,
            usd_per_1k_tokens=usd_per_1k_tokens,
        )
        with self._transaction():
            self._reload()
            self._budgets[task_id] = budget
            self._flush()
        return budget

    def get(self, task_id: str) -> Optional[TaskBudget]:
        if task_id not in self._budgets:
            self._reload()
        return self._budgets.get(task_id)

    def ensure(self, task_id: str) -> TaskBudget:
        if task_id not in self._budgets:
            self._reload()
        if task_id not in self._budgets:
            return self.create(task_id)
        return self._budgets[task_id]

    def check(self, task_id: str) -> None:
        budget = self.ensure(task_id)
        if budget.exhausted:
            raise BudgetExceeded(
                task_id,
                f"Budget exhausted: tokens={budget.used_tokens}/{budget.max_tokens} "
                f"requests={budget.used_requests}/{budget.max_requests} "
                f"cost=${budget.estimated_cost_usd:.4f}/{budget.max_cost_usd}",
            )

    def record(self, task_id: str, tokens: int = 0, requests: int = 1, cost_usd: Optional[float] = None) -> TaskBudget:
        # Apply the delta to whatever is on disk *now*, inside the lock, so a
        # sibling process's spend is added to rather than overwritten.
        with self._transaction():
            budget = self.ensure(task_id)
            budget.used_tokens += max(0, tokens)
            budget.used_requests += max(0, requests)
            if cost_usd is not None:
                budget.estimated_cost_usd += cost_usd
            else:
                budget.estimated_cost_usd += (tokens / 1000.0) * budget.usd_per_1k_tokens
            budget.updated_at = utcnow().isoformat()
            self._budgets[task_id] = budget
            self._flush()
        if budget.exhausted:
            raise BudgetExceeded(task_id, "Budget exceeded after recording usage")
        return budget

    def snapshot(self, task_id: str) -> Optional[dict]:
        b = self.get(task_id)
        return b.model_dump() if b else None
