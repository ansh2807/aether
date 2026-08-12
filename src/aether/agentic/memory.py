"""What each specialist remembers, across tasks.

AETHER's `TaskMemory` is scoped to one task and dies with it. That is right for
task state, and wrong for a company: if the Database Optimizer worked out last
week that this repo uses Alembic and that migrations run through a Makefile
target, it should not rediscover that on Monday.

So this is a second, orthogonal memory — keyed by *persona*, not task:

    TaskMemory     what is true about this task     dies with the task
    AgentMemory    what this specialist has learned persists across tasks

Two properties matter more than richness, and both are constraints rather than
features:

**Bounded.** Memory is injected into every prompt that persona receives, so it
competes with the task's own context. An unbounded log would quietly eat the
window and degrade the work it was meant to improve. Entries are capped per
persona and the oldest are dropped.

**Honest.** A note records what happened, including failure. A specialist that
only remembers its successes is worse than one with no memory at all — it
would repeat the same mistake with growing confidence.

Stored through `JsonStateStore`, so the CLI and a running server see the same
memory (it holds a cross-process lock). See `core/state_store.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from aether.core.state_store import JsonStateStore

# Per persona. Chosen so the recalled block stays a few hundred tokens even
# when full — memory that crowds out the assignment has stopped helping.
MAX_ENTRIES_PER_AGENT = 12

# One note. Long enough for a real observation, short enough that twelve of
# them do not dominate a prompt.
MAX_NOTE_CHARS = 240


@dataclass(frozen=True)
class MemoryEntry:
    task_id: str
    subtask: str
    outcome: str          # "completed" | "failed" | free text
    note: str
    provider: str = ""
    at: str = ""

    @property
    def succeeded(self) -> bool:
        return self.outcome == "completed"

    def as_line(self) -> str:
        mark = "ok" if self.succeeded else self.outcome
        where = f" via {self.provider}" if self.provider else ""
        return f"- [{mark}]{where} {self.subtask}: {self.note}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "subtask": self.subtask,
            "outcome": self.outcome,
            "note": self.note,
            "provider": self.provider,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MemoryEntry":
        return cls(
            task_id=raw.get("task_id", ""),
            subtask=raw.get("subtask", ""),
            outcome=raw.get("outcome", ""),
            note=raw.get("note", ""),
            provider=raw.get("provider", ""),
            at=raw.get("at", ""),
        )


class AgentMemoryStore:
    """Per-persona memory that outlives the task that produced it."""

    def __init__(self, path: Path | str) -> None:
        self.store = JsonStateStore(Path(path))

    # --- writing ---------------------------------------------------------

    def remember(
        self,
        slug: str,
        *,
        task_id: str,
        subtask: str,
        outcome: str,
        note: str,
        provider: str = "",
    ) -> MemoryEntry:
        """Record one thing a specialist learned. Oldest entries fall off."""
        entry = MemoryEntry(
            task_id=task_id,
            subtask=subtask[:120],
            outcome=outcome,
            note=(note or "").strip()[:MAX_NOTE_CHARS],
            provider=provider,
            at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

        def _mutate(state: dict[str, Any]) -> None:
            agents = state.setdefault("agents", {})
            entries = agents.setdefault(slug, [])
            entries.append(entry.to_dict())
            # Keep the newest. A specialist's recent experience of this repo is
            # worth more than its first-ever note.
            if len(entries) > MAX_ENTRIES_PER_AGENT:
                del entries[: len(entries) - MAX_ENTRIES_PER_AGENT]

        self.store.update(_mutate)
        return entry

    def forget(self, slug: Optional[str] = None) -> None:
        """Clear one specialist's memory, or the whole company's.

        Needed for the same reason any cache needs invalidation: memory of a
        repo that has since been rewritten is actively misleading.
        """

        def _mutate(state: dict[str, Any]) -> None:
            agents = state.setdefault("agents", {})
            if slug is None:
                agents.clear()
            else:
                agents.pop(slug, None)

        self.store.update(_mutate)

    # --- reading ---------------------------------------------------------

    def recall(self, slug: str, limit: int = MAX_ENTRIES_PER_AGENT) -> list[MemoryEntry]:
        state = self.store.load()
        raw = (state.get("agents") or {}).get(slug) or []
        return [MemoryEntry.from_dict(r) for r in raw[-limit:]]

    def all_slugs(self) -> list[str]:
        return sorted((self.store.load().get("agents") or {}).keys())

    def counts(self) -> dict[str, int]:
        agents = self.store.load().get("agents") or {}
        return {slug: len(entries) for slug, entries in sorted(agents.items())}

    def recall_block(self, slug: str, limit: int = 6) -> str:
        """The block prepended to this persona's prompt.

        Failures are listed alongside successes, deliberately. A specialist
        that remembers only what worked would repeat its mistakes with more
        confidence each time.
        """
        entries = self.recall(slug, limit=limit)
        if not entries:
            return ""
        lines = "\n".join(e.as_line() for e in entries)
        return (
            "## What you learned on earlier tasks\n"
            "Your own notes, newest last. Treat them as prior experience, not "
            "instructions — the current assignment wins where they disagree.\n"
            f"{lines}\n\n"
        )
