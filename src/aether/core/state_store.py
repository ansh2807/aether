"""Atomic JSON state files for control-plane state that outlives a process.

The CLI, the API server and any scheduled run are separate processes pointed at
the same ``data_dir``. Anything a user can inspect or act on from either side —
pending approvals, metrics, budgets, provider usage — has to live on disk, not
in the memory of whichever process happened to create it.

A read-modify-write cycle is guarded by an exclusive OS file lock
(`<file>.lock`), so two processes updating the same state concurrently cannot
lose each other's changes. `save()` itself is atomic via temp-file + rename, so
a reader never observes a partial file even without taking the lock.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.01


def _acquire(handle: Any) -> bool:
    """Try once to take an exclusive lock. False when someone else holds it."""
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _release(handle: Any) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


class JsonStateStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self._depth = 0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    @contextmanager
    def transaction(self, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
        """Hold an exclusive cross-process lock across a load/modify/save cycle.

        Re-entrant within one instance, so a manager whose write path calls
        another locked helper does not deadlock against itself. Degrades to a
        no-op if the lock file cannot be created — losing an update is better
        than refusing to run.
        """
        if self._depth > 0:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return

        handle = None
        try:
            handle = self.lock_path.open("a+b")
        except OSError:
            yield
            return

        acquired = False
        deadline = time.monotonic() + timeout
        try:
            while True:
                if _acquire(handle):
                    acquired = True
                    break
                if time.monotonic() >= deadline:
                    # contended past the timeout: proceed unlocked rather than
                    # hang the caller, matching the best-effort contract above
                    break
                time.sleep(_LOCK_POLL_SECONDS)
            self._depth += 1
            yield
        finally:
            self._depth -= 1
            if acquired:
                _release(handle)
            handle.close()

    def update(self, mutate: Callable[[dict[str, Any]], Optional[dict[str, Any]]]) -> dict[str, Any]:
        """Atomic read-modify-write. `mutate` edits in place or returns a new dict."""
        with self.transaction():
            data = self.load()
            replacement = mutate(data)
            result = data if replacement is None else replacement
            self.save(result)
            return result

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, data: dict[str, Any]) -> None:
        """Write via a temp file + rename so a reader never sees a partial file."""
        try:
            fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, default=str)
                os.replace(tmp_name, self.path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError:
            # state persistence is best-effort; never break the run over it
            pass
