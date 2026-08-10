"""Git-backed shared workspace with per-agent branches and conflict detection."""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from typing import Optional

from aether.observability.logging import get_logger

log = get_logger("aether.workspace")


class WorkspaceConflict(Exception):
    def __init__(self, files: list[str], detail: str = "") -> None:
        self.files = files
        super().__init__(detail or f"Workspace conflict on: {', '.join(files)}")


class WorkspaceManager:
    def __init__(self, enable_git: bool = True) -> None:
        self.enable_git = enable_git and bool(shutil.which("git"))

    async def _run(self, workspace: Path, *args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        text = (out or b"").decode("utf-8", errors="replace") + (err or b"").decode(
            "utf-8", errors="replace"
        )
        return proc.returncode or 0, text

    async def ensure_repo(self, workspace: Path) -> None:
        workspace.mkdir(parents=True, exist_ok=True)
        for sub in ("artifacts", "logs", "checkpoints", "memory", "agents"):
            (workspace / sub).mkdir(exist_ok=True)
        if not self.enable_git:
            return
        if not (workspace / ".git").exists():
            await self._run(workspace, "init")
            await self._run(workspace, "config", "user.email", "aether@localhost")
            await self._run(workspace, "config", "user.name", "AETHER")
            # initial commit so branches have a base
            gitignore = workspace / ".gitignore"
            if not gitignore.exists():
                gitignore.write_text("logs/\n*.pyc\n__pycache__/\n", encoding="utf-8")
            await self._run(workspace, "add", "-A")
            await self._run(workspace, "commit", "-m", "aether: init workspace", "--allow-empty")

    def agent_branch(self, provider_id: str, subtask_id: str) -> str:
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", f"{provider_id}-{subtask_id}")[:80]
        return f"aether/{safe}"

    async def checkout_agent_branch(self, workspace: Path, provider_id: str, subtask_id: str) -> str:
        await self.ensure_repo(workspace)
        branch = self.agent_branch(provider_id, subtask_id)
        if not self.enable_git:
            return branch
        # base from main/master/current
        code, _ = await self._run(workspace, "checkout", "-B", branch)
        if code != 0:
            await self._run(workspace, "checkout", "-b", branch)
        return branch

    async def commit_agent_work(
        self,
        workspace: Path,
        provider_id: str,
        message: str,
    ) -> Optional[str]:
        if not self.enable_git:
            return None
        await self._run(workspace, "add", "-A")
        await self._run(workspace, "commit", "-m", f"aether({provider_id}): {message[:160]}", "--allow-empty")
        code, out = await self._run(workspace, "rev-parse", "HEAD")
        if code == 0:
            return out.strip().splitlines()[0][:40]
        return None

    async def base_branch(self, workspace: Path) -> Optional[str]:
        """Name of the integration branch, if one exists."""
        for candidate in ("main", "master"):
            code, _ = await self._run(
                workspace, "show-ref", "--verify", "--quiet", f"refs/heads/{candidate}"
            )
            if code == 0:
                return candidate
        return None

    async def merge_to_main(self, workspace: Path, branch: str) -> tuple[bool, list[str], str]:
        """Merge agent branch into main. Returns (ok, conflict_files, detail)."""
        if not self.enable_git:
            return True, [], "git disabled"
        main = await self.base_branch(workspace)
        if main is None or main == branch:
            # nothing to integrate into (or the agent worked directly on the base)
            return True, [], "no separate base branch to merge into"

        # NOTE: plain `checkout`, never `checkout -B` — `-B` would *reset* main to
        # the agent branch tip, making the merge below a no-op and silently
        # discarding whatever else had landed on main.
        code, text = await self._run(workspace, "checkout", main)
        if code != 0:
            return False, [], f"could not switch to {main}: {text}"

        code, text = await self._run(
            workspace, "merge", "--no-ff", branch, "-m", f"aether: merge {branch}"
        )
        if code == 0:
            return True, [], text
        # conflict
        conflicts = await self.list_conflicts(workspace)
        await self._run(workspace, "merge", "--abort")
        return False, conflicts, text

    async def list_conflicts(self, workspace: Path) -> list[str]:
        if not self.enable_git:
            return []
        code, out = await self._run(workspace, "diff", "--name-only", "--diff-filter=U")
        if code != 0:
            return []
        return [line.strip() for line in out.splitlines() if line.strip()]

    async def detect_parallel_file_overlap(
        self,
        workspace: Path,
        branch_a: str,
        branch_b: str,
    ) -> list[str]:
        """Files changed on both branches relative to merge-base."""
        if not self.enable_git:
            return []
        code, base = await self._run(workspace, "merge-base", branch_a, branch_b)
        if code != 0:
            return []
        base = base.strip()
        _, files_a = await self._run(workspace, "diff", "--name-only", base, branch_a)
        _, files_b = await self._run(workspace, "diff", "--name-only", base, branch_b)
        set_a = {f.strip() for f in files_a.splitlines() if f.strip()}
        set_b = {f.strip() for f in files_b.splitlines() if f.strip()}
        return sorted(set_a & set_b)

    def index_files(self, workspace: Path, limit: int = 200) -> list[str]:
        if not workspace.exists():
            return []
        files: list[str] = []
        for p in workspace.rglob("*"):
            if p.is_file() and ".git" not in p.parts:
                try:
                    files.append(str(p.relative_to(workspace)))
                except ValueError:
                    files.append(str(p))
            if len(files) >= limit:
                break
        return files
