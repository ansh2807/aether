"""Open Code CLI adapter — multi-model coding agent via `opencode run`.

Uses the installed `opencode` CLI (https://opencode.ai). Model format:
  provider/model  e.g. opencode/kimi-k2.7-code, opencode/grok-4.5
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any, Optional

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
from aether.observability.logging import get_logger
from aether.security.sandbox import ExecutionSandbox, SecurityError

log = get_logger("aether.adapters.opencode")


def opencode_bin() -> Optional[str]:
    return shutil.which("opencode") or shutil.which("opencode.cmd")


async def list_opencode_models(timeout: float = 45.0) -> list[str]:
    """Return model ids from `opencode models`."""
    bin_path = opencode_bin()
    if not bin_path:
        return []
    try:
        proc = await asyncio.create_subprocess_exec(
            bin_path,
            "models",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        text = (out_b or b"").decode("utf-8", errors="replace")
        models: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("T ") or line.startswith("|") or line.startswith("•"):
                continue
            # model lines look like opencode/kimi-k2.7-code
            if "/" in line and " " not in line:
                models.append(line)
            elif line.startswith("opencode/") or line.startswith("opencode-go/"):
                models.append(line.split()[0])
        # de-dupe preserve order
        seen: set[str] = set()
        ordered: list[str] = []
        for m in models:
            if m not in seen:
                seen.add(m)
                ordered.append(m)
        return ordered
    except Exception as exc:  # noqa: BLE001
        log.warning("opencode_models_failed", error=str(exc))
        return []


async def opencode_auth_list(timeout: float = 30.0) -> str:
    bin_path = opencode_bin()
    if not bin_path:
        return ""
    try:
        proc = await asyncio.create_subprocess_exec(
            bin_path,
            "auth",
            "list",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return ((out_b or b"") + (err_b or b"")).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return str(exc)


class OpencodeCliAdapter(BaseAdapter):
    """Run tasks through Open Code with a selected model."""

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

    def _policy_error(self, model: str, cwd: Optional[str]) -> Optional[str]:
        """Return a denial reason when sandbox policy blocks this execution.

        Policy sees the command AETHER spawns — never the prompt, which is
        agent input rather than a command AETHER chose to run.
        """
        if self.sandbox is None:
            return None
        try:
            if cwd:
                self.sandbox.check_workspace(cwd)
            self.sandbox.check_command(f"{self.config.command or 'opencode'} run --model {model}")
        except SecurityError as exc:
            return str(exc)
        return None

    def _model(self) -> str:
        # config.model preferred; extra.model_id fallback
        return (
            self.config.model
            or (self.config.extra or {}).get("model_id")
            or "opencode/claude-sonnet-4-5"
        )

    async def authenticate(self) -> AuthStatus:
        if not opencode_bin():
            return AuthStatus(authenticated=False, method="cli", message="opencode not found on PATH")
        listing = await opencode_auth_list()
        has_creds = "credential" in listing.lower() or "api" in listing.lower() or "•" in listing
        return AuthStatus(
            authenticated=has_creds or True,  # CLI may work with free models without listed creds
            method="opencode_cli",
            message=listing.strip()[:200] or "opencode present",
        )

    async def health_check(self) -> HealthStatus:
        if not self.config.enabled:
            return HealthStatus(
                provider_id=self.provider_id,
                available=False,
                availability=ProviderAvailability.DISABLED,
                message="disabled",
                checked_at=utcnow(),
            )
        path = opencode_bin()
        if not path:
            return HealthStatus(
                provider_id=self.provider_id,
                available=False,
                availability=ProviderAvailability.UNAVAILABLE,
                message="opencode CLI not found",
                checked_at=utcnow(),
            )
        return HealthStatus(
            provider_id=self.provider_id,
            available=True,
            availability=ProviderAvailability.AVAILABLE,
            message=f"opencode at {path}; model={self._model()}",
            checked_at=utcnow(),
            details={"model": self._model(), "path": path},
        )

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        start = time.perf_counter()
        path = opencode_bin()
        if not path:
            self._record_failure()
            return ExecutionResult(
                execution_id=request.execution_id,
                provider_id=self.provider_id,
                success=False,
                error="opencode CLI not found",
                failure_class=FailureClass.CRASH,
                latency_ms=(time.perf_counter() - start) * 1000,
            )

        model = self._model()
        # Allow per-request override
        if request.metadata.get("model"):
            model = str(request.metadata["model"])

        cwd = request.workspace_path or None
        argv = [
            path,
            "run",
            "--model",
            model,
            "--format",
            "default",
            request.prompt,
        ]
        # Optional auto-approve for autonomous agent runs (sandbox still applies at AETHER level)
        if (self.config.extra or {}).get("auto", False):
            argv.insert(2, "--auto")

        denied = self._policy_error(model, cwd)
        if denied:
            self._record_failure()
            log.warning("opencode_policy_denied", provider=self.provider_id, reason=denied)
            return ExecutionResult(
                execution_id=request.execution_id,
                provider_id=self.provider_id,
                success=False,
                error=f"Blocked by AETHER security policy: {denied}",
                failure_class=FailureClass.POLICY_DENIED,
                latency_ms=(time.perf_counter() - start) * 1000,
            )

        log.info(
            "opencode_execute",
            provider=self.provider_id,
            model=model,
            execution_id=request.execution_id,
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            self._procs[request.execution_id] = proc
            self._active[request.execution_id] = True
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(),
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
                    error=f"opencode timeout after {request.timeout_seconds}s",
                    failure_class=FailureClass.TIMEOUT,
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

        if code != 0 and not stdout.strip():
            self._record_failure()
            return ExecutionResult(
                execution_id=request.execution_id,
                provider_id=self.provider_id,
                success=False,
                output=combined,
                error=f"opencode exit {code}: {stderr[:500]}",
                failure_class=self.classify_error(stderr or stdout),
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
            raw={"model": model, "exit_code": code},
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
