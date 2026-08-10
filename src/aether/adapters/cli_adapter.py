"""Generic CLI adapter — runs already-authenticated provider CLIs via subprocess.

Does NOT bypass authentication or scrape credentials.
Respects timeouts, output limits, and cancellation.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from aether.adapters.base import BaseAdapter
from aether.domain.enums import FailureClass, ProviderAvailability
from aether.domain.models import (
    AuthStatus,
    ExecutionRequest,
    ExecutionResult,
    HealthStatus,
    ProviderConfig,
    utcnow,
)
from aether.adapters.streaming import stream_process
from aether.execution_log import ExecutionLog
from aether.observability.logging import get_logger
from aether.security.sandbox import ExecutionSandbox, SecurityError

log = get_logger("aether.adapters.cli")


class CliAgentAdapter(BaseAdapter):
    """
    Spawns configured CLI with prompt via stdin or args.
    Configuration controls command, args, and prompt delivery mode.
    """

    def __init__(
        self,
        config: ProviderConfig,
        sandbox: Optional[ExecutionSandbox] = None,
        max_output_bytes: int = 2_000_000,
    ) -> None:
        super().__init__(config)
        self.sandbox = sandbox
        self.max_output_bytes = max_output_bytes
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    def _command_path(self) -> Optional[str]:
        cmd = self.config.command
        if not cmd:
            return None
        return shutil.which(cmd) or (cmd if Path(cmd).exists() else None)

    def _policy_command(self) -> str:
        """Command line the sandbox policy is evaluated against.

        Only the configured command and its static args — never the prompt.
        The prompt is agent input, not something AETHER chose to execute.
        """
        return " ".join([self.config.command, *list(self.config.args or [])]).strip()

    def _policy_error(self, cwd: Optional[str]) -> Optional[str]:
        """Return a denial reason when sandbox policy blocks this execution."""
        if self.sandbox is None:
            return None
        try:
            if cwd:
                self.sandbox.check_workspace(cwd)
            self.sandbox.check_command(self._policy_command())
        except SecurityError as exc:
            return str(exc)
        return None

    async def authenticate(self) -> AuthStatus:
        # CLI auth is external — we only check presence
        path = self._command_path()
        if not path:
            return AuthStatus(
                authenticated=False,
                method="cli",
                message=f"CLI not found: {self.config.command}",
            )
        return AuthStatus(
            authenticated=True,
            method="cli_external",
            message="CLI present; using machine-local authentication if already configured",
        )

    async def health_check(self) -> HealthStatus:
        path = self._command_path()
        if not self.config.enabled:
            return HealthStatus(
                provider_id=self.provider_id,
                available=False,
                availability=ProviderAvailability.DISABLED,
                message="disabled in config",
                checked_at=utcnow(),
            )
        if not path:
            return HealthStatus(
                provider_id=self.provider_id,
                available=False,
                availability=ProviderAvailability.UNAVAILABLE,
                message=f"CLI unavailable: command '{self.config.command}' not found",
                checked_at=utcnow(),
            )
        auth = await self.authenticate()
        return HealthStatus(
            provider_id=self.provider_id,
            available=True,
            availability=ProviderAvailability.AVAILABLE,
            message=f"CLI found at {path}; {auth.message}",
            checked_at=utcnow(),
            details={"path": path},
        )

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        start = time.perf_counter()
        path = self._command_path()
        if not path:
            self._record_failure()
            return ExecutionResult(
                execution_id=request.execution_id,
                provider_id=self.provider_id,
                success=False,
                error=f"CLI not found: {self.config.command}",
                failure_class=FailureClass.CRASH,
                latency_ms=(time.perf_counter() - start) * 1000,
            )

        prompt = request.prompt
        if request.context_package:
            # context already inlined by orchestrator usually
            pass

        # Build argv — configurable; default: command + args, prompt on stdin
        argv = [path, *list(self.config.args or [])]
        prompt_mode = (self.config.extra or {}).get("prompt_mode", "stdin")
        if prompt_mode == "arg":
            argv.append(prompt)
        elif prompt_mode == "flag":
            flag = (self.config.extra or {}).get("prompt_flag", "-p")
            argv.extend([flag, prompt])

        cwd = request.workspace_path or None
        env = os.environ.copy()
        # Never inject secrets from task memory; only pass through process env.
        # Optionally allow provider-specific env passthrough names from config.
        for env_name in (self.config.extra or {}).get("env_passthrough", []) or []:
            if env_name in os.environ:
                env[env_name] = os.environ[env_name]

        denied = self._policy_error(cwd)
        if denied:
            self._record_failure()
            log.warning("cli_policy_denied", provider=self.provider_id, reason=denied)
            return ExecutionResult(
                execution_id=request.execution_id,
                provider_id=self.provider_id,
                success=False,
                error=f"Blocked by AETHER security policy: {denied}",
                failure_class=FailureClass.POLICY_DENIED,
                latency_ms=(time.perf_counter() - start) * 1000,
            )

        log.info(
            "cli_execute",
            provider=self.provider_id,
            execution_id=request.execution_id,
            argv0=argv[0],
            cwd=cwd,
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if prompt_mode == "stdin" else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            self._procs[request.execution_id] = proc
            self._active[request.execution_id] = True

            stdin_data = prompt.encode("utf-8") if prompt_mode == "stdin" else None
            # Read as the agent writes, mirroring stdout into the execution log.
            # communicate() would buffer everything until exit, so a kill at
            # minute nine of a ten-minute agent would lose all of it.
            exec_log = ExecutionLog.for_request(request.workspace_path, request.subtask_id)
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    stream_process(proc, stdin_data, self.max_output_bytes, exec_log),
                    timeout=request.timeout_seconds,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                self._record_failure()
                return ExecutionResult(
                    execution_id=request.execution_id,
                    provider_id=self.provider_id,
                    success=False,
                    error=f"CLI timeout after {request.timeout_seconds}s",
                    failure_class=FailureClass.TIMEOUT,
                    latency_ms=(time.perf_counter() - start) * 1000,
                )
        except FileNotFoundError as exc:
            self._record_failure()
            return ExecutionResult(
                execution_id=request.execution_id,
                provider_id=self.provider_id,
                success=False,
                error=str(exc),
                failure_class=FailureClass.CRASH,
                latency_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:  # noqa: BLE001
            self._record_failure()
            return ExecutionResult(
                execution_id=request.execution_id,
                provider_id=self.provider_id,
                success=False,
                error=str(exc),
                failure_class=self.classify_error(str(exc)),
                latency_ms=(time.perf_counter() - start) * 1000,
            )
        finally:
            self._procs.pop(request.execution_id, None)
            self._active.pop(request.execution_id, None)

        stdout = (stdout_b or b"")[: self.max_output_bytes].decode("utf-8", errors="replace")
        stderr = (stderr_b or b"")[: self.max_output_bytes].decode("utf-8", errors="replace")
        combined = stdout if stdout.strip() else stderr
        code = proc.returncode or 0

        if code != 0:
            fc = self.classify_error(stderr or stdout)
            # auth/rate patterns in stderr
            self._record_failure()
            handoff = self.parse_handoff(
                request.task_id, self.provider_id, combined, requested=request.prompt[:300]
            )
            return ExecutionResult(
                execution_id=request.execution_id,
                provider_id=self.provider_id,
                success=False,
                output=combined,
                handoff=handoff,
                error=f"CLI exit {code}: {(stderr or stdout)[:500]}",
                failure_class=fc,
                latency_ms=(time.perf_counter() - start) * 1000,
            )

        handoff = self.parse_handoff(
            request.task_id, self.provider_id, combined, requested=request.prompt[:300]
        )
        self._record_request(tokens=len(combined) // 4)
        return ExecutionResult(
            execution_id=request.execution_id,
            provider_id=self.provider_id,
            success=True,
            output=combined,
            handoff=handoff,
            latency_ms=(time.perf_counter() - start) * 1000,
        )

    async def cancel(self, execution_id: str) -> None:
        proc = self._procs.get(execution_id)
        if proc and proc.returncode is None:
            proc.kill()
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
        await super().cancel(execution_id)

    async def cancel_all(self) -> None:
        for execution_id in list(self._procs.keys()):
            await self.cancel(execution_id)
        await super().cancel_all()
