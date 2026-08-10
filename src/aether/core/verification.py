"""Verification engine — never trust agent 'done' claims blindly."""

from __future__ import annotations

import asyncio
import ast
import py_compile
import shutil
from pathlib import Path
from typing import Optional

from aether.domain.enums import VerificationStatus
from aether.domain.models import Task, VerificationCheck, VerificationResult
from aether.security.sandbox import ExecutionSandbox, SecurityError
from aether.storage.repository import SqliteRepository


class VerificationEngine:
    def __init__(self, repo: SqliteRepository, sandbox: Optional[ExecutionSandbox] = None) -> None:
        self.repo = repo
        self.sandbox = sandbox

    def _policy_error(self, command: str, cwd: Path) -> Optional[str]:
        """Return a denial reason when sandbox policy blocks a verification command."""
        if self.sandbox is None:
            return None
        try:
            self.sandbox.check_workspace(cwd)
            self.sandbox.check_command(command)
        except SecurityError as exc:
            return str(exc)
        return None

    async def verify_task(self, task: Task) -> VerificationResult:
        checks: list[VerificationCheck] = []
        ws = Path(task.workspace_path) if task.workspace_path else None

        if ws is None or not ws.exists():
            checks.append(
                VerificationCheck(
                    name="workspace_exists",
                    status=VerificationStatus.FAILED,
                    detail="Workspace missing",
                )
            )
            result = VerificationResult(
                task_id=task.task_id,
                status=VerificationStatus.FAILED,
                checks=checks,
                summary="Workspace missing",
            )
            await self.repo.save_result(result)
            return result

        checks.append(
            VerificationCheck(
                name="workspace_exists",
                status=VerificationStatus.PASSED,
                detail=str(ws),
            )
        )

        # files present
        files = [p for p in ws.rglob("*") if p.is_file() and ".git" not in p.parts]
        if not files:
            checks.append(
                VerificationCheck(
                    name="artifacts_present",
                    status=VerificationStatus.FAILED,
                    detail="No files produced in workspace",
                )
            )
        else:
            checks.append(
                VerificationCheck(
                    name="artifacts_present",
                    status=VerificationStatus.PASSED,
                    detail=f"{len(files)} files",
                )
            )

        # Python syntax check
        py_files = [p for p in files if p.suffix == ".py"]
        py_ok = True
        py_details: list[str] = []
        for pf in py_files[:50]:
            try:
                py_compile.compile(str(pf), doraise=True)
            except Exception as exc:  # noqa: BLE001
                py_ok = False
                py_details.append(f"{pf.name}: {exc}")
        if py_files:
            checks.append(
                VerificationCheck(
                    name="python_syntax",
                    status=VerificationStatus.PASSED if py_ok else VerificationStatus.FAILED,
                    detail="; ".join(py_details) if py_details else f"{len(py_files)} files ok",
                )
            )

        # AST parse as secondary
        if py_files and py_ok:
            for pf in py_files[:20]:
                try:
                    ast.parse(pf.read_text(encoding="utf-8", errors="replace"))
                except SyntaxError as exc:
                    py_ok = False
                    checks.append(
                        VerificationCheck(
                            name="python_ast",
                            status=VerificationStatus.FAILED,
                            detail=str(exc),
                        )
                    )
                    break

        # pytest if present
        if (ws / "tests").exists() or any("test_" in p.name for p in py_files):
            denied = self._policy_error("pytest -q --tb=no", ws)
            if denied:
                checks.append(
                    VerificationCheck(
                        name="pytest",
                        status=VerificationStatus.SKIPPED,
                        detail=f"blocked by security policy: {denied}",
                    )
                )
            elif shutil.which("pytest"):
                code, out = await self._run(["pytest", "-q", "--tb=no"], cwd=ws, timeout=120)
                checks.append(
                    VerificationCheck(
                        name="pytest",
                        status=VerificationStatus.PASSED if code == 0 else VerificationStatus.FAILED,
                        detail=out[:500],
                    )
                )
            else:
                checks.append(
                    VerificationCheck(
                        name="pytest",
                        status=VerificationStatus.SKIPPED,
                        detail="pytest not installed",
                    )
                )

        # research / non-code: require some markdown or notes
        goal_l = (task.normalized_goal or task.original_prompt).lower()
        if any(k in goal_l for k in ("research", "analyze", "summarize")):
            md = [p for p in files if p.suffix in {".md", ".txt", ".json"}]
            checks.append(
                VerificationCheck(
                    name="research_artifact",
                    status=VerificationStatus.PASSED if md else VerificationStatus.PARTIAL,
                    detail=f"{len(md)} text artifacts",
                )
            )

        status = self._aggregate(checks)
        result = VerificationResult(
            task_id=task.task_id,
            status=status,
            checks=checks,
            summary=f"{status.value}: {len(checks)} checks",
        )
        await self.repo.save_result(result)
        return result

    def _aggregate(self, checks: list[VerificationCheck]) -> VerificationStatus:
        actionable = [c for c in checks if c.status != VerificationStatus.SKIPPED]
        if not actionable:
            return VerificationStatus.SKIPPED
        if all(c.status == VerificationStatus.PASSED for c in actionable):
            return VerificationStatus.PASSED
        if any(c.status == VerificationStatus.FAILED for c in actionable):
            # partial if some passed
            if any(c.status == VerificationStatus.PASSED for c in actionable):
                return VerificationStatus.FAILED
            return VerificationStatus.FAILED
        return VerificationStatus.PARTIAL

    async def _run(self, argv: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode or 0, (out_b or b"").decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return 1, str(exc)
