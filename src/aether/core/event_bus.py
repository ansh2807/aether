"""In-process event bus with ring-buffer history for observability."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Awaitable, Callable, Deque, Optional

from aether.domain.models import utcnow

Listener = Callable[[dict[str, Any]], Any | Awaitable[Any]]


class EventBus:
    def __init__(self, history_size: int = 2000) -> None:
        self._listeners: list[Listener] = []
        self._history: Deque[dict[str, Any]] = deque(maxlen=history_size)
        self._lock = asyncio.Lock()

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: Listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    async def emit(self, event_type: str, **payload: Any) -> dict[str, Any]:
        event: dict[str, Any] = {
            "type": event_type,
            "timestamp": utcnow().isoformat(),
            **payload,
        }
        self._history.append(event)
        for listener in list(self._listeners):
            try:
                result = listener(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                pass
        return event

    def history(
        self,
        task_id: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        items = list(self._history)
        if task_id:
            items = [e for e in items if e.get("task_id") == task_id]
        if event_type:
            items = [e for e in items if e.get("type") == event_type]
        return items[-limit:]
