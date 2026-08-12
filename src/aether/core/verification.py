"""Verification engine — never trust agent 'done' claims blindly."""

from __future__ import annotations

import ast
import asyncio
import os
import py_compile
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from aether.domain.enums import VerificationStatus
from aether.domain.models import Task, VerificationCheck, VerificationResult
from aether.security.sandbox import ExecutionSandbox, SecurityError
from aether.storage.repository import SqliteRepository

# The supported local image is built from digest-pinned base images and contains
# hash-pinned pytest tooling. Custom production images should be referenced by
# digest after they are built and scanned.
CONTAINER_IMAGE = "aether:local"

# The only variables a verification command has any business seeing. An
# allowlist, not a denylist: a new provider adding FOO_API_KEY must not silently
# become readable by agent-written code because nobody remembered to block it.
_ENV_ALLOWLIST = (
    "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE",
    "LANG", "LC_ALL", "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONHASHSEED",
)


RESEARCH_WORDS = ("research", "analyz", "analys", "compare", "survey", "investigate", "summari")

# A claim without a source is the failure mode research tasks actually have.
_SOURCE_RE = re.compile(r"https?://\S+|\[\d+\]|\bdoi:|\bsources?:|\breferences?\b", re.IGNORECASE)
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "about", "a", "an",
    "of", "to", "in", "on", "is", "are", "be", "how", "what", "why", "which", "best",
}


def _is_research(task: Task) -> bool:
    goal = (task.normalized_goal or task.original_prompt or "").lower()
    return any(word in goal for word in RESEARCH_WORDS)


def _scrubbed_env() -> dict[str, str]:
    """Just enough environment to run a test, and nothing else."""
    env = {name: os.environ[name] for name in _ENV_ALLOWLIST if name in os.environ}
    env["AETHER_VERIFICATION"] = "1"   # so a test can tell it is being verified
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


class VerificationEngine:
    def __init__(
        self,
        repo: SqliteRepository,
        sandbox: ExecutionSandbox | None = None,
        sandbox_mode: str = "container",
        allow_container_fallback: bool = False,
        container_image: str = CONTAINER_IMAGE,
    ) -> None:
        self.repo = repo
        self.sandbox = sandbox
        self.sandbox_mode = sandbox_mode
        self.allow_container_fallback = allow_container_fallback
        self.container_image = container_image

    def _policy_error(self, command: str, cwd: Path) -> str | None:
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

        # Symlinks can point outside the task workspace and turn verification
        # into an arbitrary host-file reader. Do not follow or accept them.
        symlinks = [p for p in ws.rglob("*") if p.is_symlink() and ".git" not in p.parts]
        if symlinks:
            checks.append(
                VerificationCheck(
                    name="workspace_symlinks",
                    status=VerificationStatus.FAILED,
                    detail=f"{len(symlinks)} symlink(s) are not allowed in verification workspaces",
                )
            )

        # files present
        files = [
            p
            for p in ws.rglob("*")
            if p.is_file() and not p.is_symlink() and ".git" not in p.parts
        ]
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

        # Secure code review of what the agents just wrote. Existence and
        # syntax were already checked; neither notices that a file shipping a
        # hardcoded key or `shell=True` compiles perfectly.
        from aether.skills.cyber import blocking, scan_workspace, summarise

        sec_findings = scan_workspace(ws)
        if sec_findings:
            severe = blocking(sec_findings)
            checks.append(
                VerificationCheck(
                    name="secure_code_review",
                    status=VerificationStatus.FAILED if severe else VerificationStatus.PASSED,
                    detail=summarise(sec_findings),
                )
            )
        else:
            checks.append(
                VerificationCheck(
                    name="secure_code_review",
                    status=VerificationStatus.PASSED,
                    detail="no findings",
                )
            )

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
                out = f"[sandbox={self.sandbox_mode}] {out}"
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

        if _is_research(task):
            checks.extend(self._verify_research(task, files))

        status = self._aggregate(checks)
        result = VerificationResult(
            task_id=task.task_id,
            status=status,
            checks=checks,
            summary=f"{status.value}: {len(checks)} checks",
        )
        await self.repo.save_result(result)
        return result

    def _verify_research(self, task: Task, files: list[Path]) -> list[VerificationCheck]:
        """Verification that fits a research task.

        "A .md file exists" was the old bar, which passes for an empty file.
        What actually makes research usable is that it is sourced and that it
        answers the question that was asked.
        """
        checks: list[VerificationCheck] = []
        prose = [p for p in files if p.suffix.lower() in {".md", ".txt", ".rst"}]
        if not prose:
            return [
                VerificationCheck(
                    name="research_output",
                    status=VerificationStatus.FAILED,
                    detail="no written output was produced",
                )
            ]

        body = ""
        for path in prose[:20]:
            try:
                body += path.read_text(encoding="utf-8", errors="replace") + "\n"
            except OSError:
                continue

        checks.append(
            VerificationCheck(
                name="research_output",
                status=VerificationStatus.PASSED,
                detail=f"{len(prose)} document(s), {len(body)} chars",
            )
        )

        sources = _SOURCE_RE.findall(body)
        checks.append(
            VerificationCheck(
                name="research_sources",
                status=VerificationStatus.PASSED if sources else VerificationStatus.FAILED,
                detail=(
                    f"{len(sources)} citation(s)"
                    if sources
                    else "no citations, links or references — the findings cannot be checked"
                ),
            )
        )

        # Does the output actually engage with what was asked?
        goal = (task.normalized_goal or task.original_prompt or "").lower()
        terms = {
            w.strip(".,:;?!()[]\"'")
            for w in goal.split()
            if len(w) > 3 and w.lower() not in _STOPWORDS
        }
        lowered = body.lower()
        covered = {t for t in terms if t and t in lowered}
        ratio = len(covered) / len(terms) if terms else 1.0
        checks.append(
            VerificationCheck(
                name="research_addresses_question",
                status=(
                    VerificationStatus.PASSED
                    if ratio >= 0.5
                    else VerificationStatus.PARTIAL
                    if ratio > 0
                    else VerificationStatus.FAILED
                ),
                detail=f"{len(covered)}/{len(terms)} key terms from the goal appear in the output",
            )
        )
        return checks

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

    def _container_runtime(self) -> str | None:
        return shutil.which("docker") or shutil.which("podman")

    def _image_missing(self, runtime: str) -> bool:
        """True when the runtime is present but the verifier image is not built.

        Distinguished from "no runtime" because the two need different actions
        and the raw failure is unhelpful: without this check the run reaches
        the daemon and comes back with Docker's own "Unable to find image ...
        pull access denied", which reads like a registry permissions problem
        and never mentions that the image is built locally with one command.
        """
        try:
            probe = subprocess.run(  # noqa: S603
                [runtime, "image", "inspect", self.container_image],
                capture_output=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            # Cannot tell — let the real run report whatever is actually wrong
            # rather than blocking on a guess.
            return False
        return probe.returncode != 0

    def _containerise(self, argv: list[str], cwd: Path) -> tuple[list[str], str] | None:
        """Wrap the command so it runs with no network and capped resources."""
        runtime = self._container_runtime()
        if runtime is None:
            return None
        name = f"aether-verify-{uuid.uuid4().hex}"
        if hasattr(os, "getuid") and hasattr(os, "getgid"):
            uid, gid = os.getuid(), os.getgid()
            user = "65534:65534" if uid == 0 else f"{uid}:{gid}"
        else:
            # Docker Desktop maps bind-mounted Windows files independently of
            # the Linux UID, so an unprivileged numeric identity remains able
            # to write verification artefacts without becoming root.
            user = "65534:65534"
        return ([
            runtime, "run", "--rm",
            "--name", name,
            "--network", "none",          # no exfiltration, no dependency fetch
            "--memory", "512m",
            "--memory-swap", "512m",
            "--cpus", "1",
            "--pids-limit", "128",
            "--ulimit", "nofile=1024:1024",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges=true",
            "--ipc", "none",
            "--user", user,
            "--read-only",                # only the workspace mount is writable
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
            "--env", "AETHER_VERIFICATION=1",
            "--env", "HOME=/tmp",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "TMPDIR=/tmp",
            "-v", f"{cwd.resolve()}:/work",
            "-w", "/work",
            self.container_image,
            *argv,
        ], name)

    async def _force_remove_container(self, runtime: str, name: str) -> None:
        """Best-effort cleanup when the attached Docker client is interrupted."""
        cleanup: asyncio.subprocess.Process | None = None
        try:
            cleanup = await asyncio.create_subprocess_exec(
                runtime,
                "rm",
                "--force",
                name,
                env=_scrubbed_env(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(cleanup.wait(), timeout=10)
        except Exception:  # noqa: BLE001 -- cleanup must not hide the original failure
            if cleanup is not None and cleanup.returncode is None:
                cleanup.kill()
                await cleanup.wait()

    async def _run(self, argv: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
        """Run a verification command over agent-written code.

        The code being verified is the code we do not trust — that is the whole
        premise of the gate — so it must not inherit the operator's environment.
        Before this was scrubbed, an agent-written test could simply read
        os.environ and walk off with every provider API key on the machine.
        """
        mode = (self.sandbox_mode or "subprocess").lower()
        env: dict[str, str] | None = None
        note = ""
        container: tuple[str, str] | None = None

        if mode == "container":
            runtime = self._container_runtime()
            if runtime is not None and self._image_missing(runtime):
                # Fail closed like any other unusable sandbox, but say the one
                # thing that fixes it. This is the common developer case:
                # Docker installed, verifier image never built.
                if not self.allow_container_fallback:
                    return (
                        126,
                        (
                            f"verification blocked: container sandbox requested but the "
                            f"image '{self.container_image}' is not built. Build it with:\n"
                            f"    docker build -t {self.container_image} .\n"
                            f"Or set security.verification_allow_fallback=true only for "
                            f"trusted workspaces."
                        ),
                    )
                note = (
                    f"verifier image '{self.container_image}' not built; explicit "
                    f"fallback enabled; ran with a scrubbed environment instead\n"
                )
                mode = "subprocess"

        if mode == "container":
            wrapped = self._containerise(argv, cwd)
            if wrapped is None:
                if not self.allow_container_fallback:
                    return (
                        126,
                        (
                            "verification blocked: container sandbox requested but "
                            "Docker/Podman is unavailable; set "
                            "security.verification_allow_fallback=true only for trusted "
                            "workspaces"
                        ),
                    )
                note = (
                    "container runtime not found; explicit fallback enabled; "
                    "ran with a scrubbed environment instead\n"
                )
                mode = "subprocess"
            else:
                argv, container_name = wrapped
                container = (argv[0], container_name)

        if mode != "off":
            env = _scrubbed_env()

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode or 0, note + (out_b or b"").decode("utf-8", errors="replace")
        except TimeoutError:
            # never leave the child running orphaned past the timeout
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            if container is not None:
                await self._force_remove_container(*container)
            return 1, f"{note}timed out after {timeout}s"
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            if container is not None:
                await self._force_remove_container(*container)
            raise
        except Exception as exc:  # noqa: BLE001
            if container is not None:
                await self._force_remove_container(*container)
            return 1, f"{note}{exc}"
