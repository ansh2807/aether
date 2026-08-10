"""Checkpoint manager with optional git commits."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Optional

from aether.config.settings import AetherSettings
from aether.domain.models import Checkpoint, Task, TaskMemory
from aether.observability.logging import get_logger
from aether.storage.repository import SqliteRepository

log = get_logger("aether.checkpoint")


class CheckpointManager:
    def __init__(self, settings: AetherSettings, repo: SqliteRepository) -> None:
        self.settings = settings
        self.repo = repo

    async def create(
        self,
        task: Task,
        memory: TaskMemory,
        agent_id: Optional[str] = None,
        state_summary: str = "",
        tests_summary: str = "",
        next_action: str = "",
    ) -> Checkpoint:
        git_commit = None
        if self.settings.orchestrator.enable_git_checkpoints and task.workspace_path:
            git_commit = await self._git_checkpoint(Path(task.workspace_path), state_summary or "aether checkpoint")

        cp = Checkpoint(
            task_id=task.task_id,
            agent_id=agent_id,
            git_commit=git_commit,
            state_summary=state_summary or memory.current_state,
            tests_summary=tests_summary,
            next_action=next_action or memory.next_action,
            memory_snapshot=memory.model_copy(deep=True),
        )
        await self.repo.save_checkpoint(cp)
        log.info("checkpoint_created", task_id=task.task_id, checkpoint_id=cp.checkpoint_id, git=git_commit)
        return cp

    async def _git_checkpoint(self, workspace: Path, message: str) -> Optional[str]:
        if not shutil.which("git"):
            return None
        workspace.mkdir(parents=True, exist_ok=True)

        async def run(*args: str) -> tuple[int, str]:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            text = (out or b"").decode("utf-8", errors="replace") + (err or b"").decode("utf-8", errors="replace")
            return proc.returncode or 0, text

        if not (workspace / ".git").exists():
            code, _ = await run("init")
            if code != 0:
                return None
            await run("config", "user.email", "aether@localhost")
            await run("config", "user.name", "AETHER")

        await run("add", "-A")
        code, _ = await run("commit", "-m", message[:200], "--allow-empty")
        if code != 0:
            # maybe nothing to commit without allow-empty on older git — try status
            pass
        code, out = await run("rev-parse", "HEAD")
        if code == 0:
            return out.strip().splitlines()[0][:40]
        return None

    async def list(self, task_id: str) -> list[Checkpoint]:
        return await self.repo.list_checkpoints(task_id)

    async def latest(self, task_id: str) -> Optional[Checkpoint]:
        return await self.repo.latest_checkpoint(task_id)
