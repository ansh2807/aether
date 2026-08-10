"""Append-only capture of agent output as it arrives.

Adapters buffer a whole response in memory before returning it. Kill the process
at minute nine of a ten-minute agent and everything is gone — the largest hole in
the claim that AETHER does not lose work.

Writing incrementally means a killed run leaves everything received up to that
moment on disk, under the task's own workspace, where recovery can salvage it
into a partial handoff instead of restarting the subtask from zero.

The file is deliberately plain text: if AETHER itself cannot recover it, a human
can still read what the agent had produced.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

LOG_DIRNAME = "logs"
SUFFIX = ".partial"


class ExecutionLog:
    """The in-flight output of one subtask, on disk."""

    def __init__(self, workspace: Path | str, subtask_id: str) -> None:
        self.subtask_id = subtask_id
        self.path = Path(workspace) / LOG_DIRNAME / f"exec-{subtask_id}{SUFFIX}"
        self._ready = False

    @classmethod
    def for_request(cls, workspace_path: str, subtask_id: Optional[str]) -> Optional["ExecutionLog"]:
        """A log for this execution, or None when there is nothing to key it to.

        Chat turns have no subtask and are not resumable work, so they are not
        logged here.
        """
        if not workspace_path or not subtask_id:
            return None
        return cls(workspace_path, subtask_id)

    def _ensure_dir(self) -> bool:
        if self._ready:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._ready = True
        except OSError:
            return False
        return True

    def append(self, text: str) -> None:
        """Add output. Best-effort: capture must never break the run."""
        if not text or not self._ensure_dir():
            return
        try:
            with self.path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(text)
                handle.flush()
        except OSError:
            pass

    def exists(self) -> bool:
        try:
            return self.path.is_file() and self.path.stat().st_size > 0
        except OSError:
            return False

    def read(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def clear(self) -> None:
        """Drop the log once its work has been accounted for."""
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
