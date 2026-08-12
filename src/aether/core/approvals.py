"""Human-in-the-loop approval queue.

Tasks can block in WAITING until an operator grants or denies an approval.
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from aether.core.state_store import JsonStateStore
from aether.domain.models import new_id, utcnow


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    GRANTED = "GRANTED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"


class ApprovalRequest(BaseModel):
    approval_id: str = Field(default_factory=lambda: new_id("apr-"))
    task_id: str
    operation: str
    detail: str = ""
    risk: str = "medium"  # low | medium | high
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_by: str = "orchestrator"
    created_at: str = Field(default_factory=lambda: utcnow().isoformat())
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApprovalManager:
    """Async approval gate with optional auto-grant for low-risk ops.

    Backed by a JSON state file when one is supplied, so a gate raised by the
    server can be listed and resolved from the CLI (and vice versa).
    """

    def __init__(
        self,
        auto_grant_low_risk: bool = False,
        store: Optional["JsonStateStore"] = None,
    ) -> None:
        self.auto_grant_low_risk = auto_grant_low_risk
        self.store = store
        self._pending: dict[str, ApprovalRequest] = {}
        self._history: list[ApprovalRequest] = []
        self._events: dict[str, asyncio.Event] = {}
        self._reload()

    # --- persistence -----------------------------------------------------

    def _transaction(self):
        """Exclusive lock across reload+mutate+flush; no-op without a store."""
        if self.store is None:
            from contextlib import nullcontext

            return nullcontext()
        return self.store.transaction()

    def _reload(self) -> None:
        if self.store is None:
            return
        data = self.store.load()
        self._pending = {
            item["approval_id"]: ApprovalRequest.model_validate(item)
            for item in data.get("pending", [])
        }
        self._history = [
            ApprovalRequest.model_validate(item) for item in data.get("history", [])
        ]

    def _flush(self) -> None:
        if self.store is None:
            return
        self.store.save(
            {
                "pending": [a.model_dump(mode="json") for a in self._pending.values()],
                "history": [a.model_dump(mode="json") for a in self._history[-500:]],
            }
        )

    # --- queries ---------------------------------------------------------

    def list_pending(self, task_id: Optional[str] = None) -> list[ApprovalRequest]:
        self._reload()
        items = [a for a in self._pending.values() if a.status == ApprovalStatus.PENDING]
        if task_id:
            items = [a for a in items if a.task_id == task_id]
        return sorted(items, key=lambda a: a.created_at)

    def list_all(self, limit: int = 100) -> list[ApprovalRequest]:
        self._reload()
        return (self._history + list(self._pending.values()))[-limit:]

    def get(self, approval_id: str) -> Optional[ApprovalRequest]:
        self._reload()
        if approval_id in self._pending:
            return self._pending[approval_id]
        for a in reversed(self._history):
            if a.approval_id == approval_id:
                return a
        return None

    async def request(
        self,
        task_id: str,
        operation: str,
        detail: str = "",
        risk: str = "medium",
        timeout_seconds: Optional[float] = None,
        requested_by: str = "orchestrator",
        metadata: Optional[dict[str, Any]] = None,
    ) -> ApprovalRequest:
        req = ApprovalRequest(
            task_id=task_id,
            operation=operation,
            detail=detail,
            risk=risk,
            requested_by=requested_by,
            metadata=metadata or {},
        )
        with self._transaction():
            self._reload()
            if self.auto_grant_low_risk and risk == "low":
                req.status = ApprovalStatus.GRANTED
                req.resolved_at = utcnow().isoformat()
                req.resolved_by = "auto"
                self._history.append(req)
                self._flush()
                return req
            self._pending[req.approval_id] = req
            self._flush()

        event = asyncio.Event()
        self._events[req.approval_id] = event

        if timeout_seconds is None:
            # non-blocking request — caller can poll
            return req

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            with self._transaction():
                self._reload()
                req.status = ApprovalStatus.EXPIRED
                req.resolved_at = utcnow().isoformat()
                req.resolved_by = "timeout"
                self._pending.pop(req.approval_id, None)
                self._history.append(req)
                self._events.pop(req.approval_id, None)
                self._flush()
            return req

        return self.get(req.approval_id) or req

    def resolve(
        self,
        approval_id: str,
        granted: bool,
        resolved_by: str = "user",
    ) -> ApprovalRequest:
        with self._transaction():
            self._reload()
            req = self._pending.get(approval_id)
            if req is None:
                raise KeyError(f"Approval not found or already resolved: {approval_id}")
            req.status = ApprovalStatus.GRANTED if granted else ApprovalStatus.DENIED
            req.resolved_at = utcnow().isoformat()
            req.resolved_by = resolved_by
            self._pending.pop(approval_id, None)
            self._history.append(req)
            self._flush()
        event = self._events.pop(approval_id, None)
        if event:
            event.set()
        return req

    async def wait_for(self, approval_id: str, timeout_seconds: float = 3600) -> ApprovalRequest:
        event = self._events.get(approval_id)
        if event is None:
            existing = self.get(approval_id)
            if existing:
                return existing
            raise KeyError(approval_id)
        if self.store is None:
            await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
        else:
            # another process may resolve this — the local Event never fires for
            # a cross-process grant, so poll the shared state as well
            deadline = asyncio.get_event_loop().time() + timeout_seconds
            while asyncio.get_event_loop().time() < deadline:
                if event.is_set():
                    break
                current = self.get(approval_id)
                if current is not None and current.status != ApprovalStatus.PENDING:
                    return current
                try:
                    await asyncio.wait_for(event.wait(), timeout=1.0)
                    break
                except asyncio.TimeoutError:
                    continue
            else:
                raise asyncio.TimeoutError(approval_id)
        return self.get(approval_id) or ApprovalRequest(
            approval_id=approval_id, task_id="", operation="", status=ApprovalStatus.EXPIRED
        )
