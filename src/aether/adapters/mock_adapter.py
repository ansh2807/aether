"""Mock adapter for testing and offline demos.

NOT a real provider. Clearly labeled for_testing.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator, Optional

from aether.adapters.base import BaseAdapter
from aether.domain.enums import FailureClass, ProviderAvailability
from aether.domain.models import (
    AuthStatus,
    ExecutionRequest,
    ExecutionResult,
    HealthStatus,
    ProviderConfig,
    StreamChunk,
    utcnow,
)


class MockAdapter(BaseAdapter):
    """Deterministic fake agent used in tests and demo mode."""

    def __init__(self, config: ProviderConfig, behavior: Optional[dict[str, Any]] = None) -> None:
        super().__init__(config)
        self.behavior = behavior or {}
        self._fail_next: Optional[FailureClass] = None
        self._call_count = 0

    def set_fail_next(self, failure: FailureClass | None) -> None:
        self._fail_next = failure

    async def authenticate(self) -> AuthStatus:
        if self.behavior.get("auth_fail"):
            return AuthStatus(authenticated=False, method="mock", message="mock auth failure")
        return AuthStatus(authenticated=True, method="mock", message="mock ok")

    async def health_check(self) -> HealthStatus:
        if self.behavior.get("unavailable"):
            return HealthStatus(
                provider_id=self.provider_id,
                available=False,
                availability=ProviderAvailability.UNAVAILABLE,
                message="mock unavailable",
                checked_at=utcnow(),
            )
        return HealthStatus(
            provider_id=self.provider_id,
            available=True,
            availability=ProviderAvailability.AVAILABLE,
            latency_ms=1.0,
            message="mock healthy",
            checked_at=utcnow(),
        )

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        start = time.perf_counter()
        self._call_count += 1
        self._active[request.execution_id] = True

        delay = float(self.behavior.get("delay", 0.05))
        await asyncio.sleep(delay)

        if self._fail_next is not None:
            fc = self._fail_next
            self._fail_next = None
            self._record_failure()
            self._active.pop(request.execution_id, None)
            return ExecutionResult(
                execution_id=request.execution_id,
                provider_id=self.provider_id,
                success=False,
                error=f"mock failure: {fc.value}",
                failure_class=fc,
                latency_ms=(time.perf_counter() - start) * 1000,
            )

        # scripted failures by call index
        fail_on = self.behavior.get("fail_on_calls") or []
        if self._call_count in fail_on:
            fc = FailureClass(self.behavior.get("fail_class", FailureClass.TIMEOUT.value))
            self._record_failure()
            self._active.pop(request.execution_id, None)
            return ExecutionResult(
                execution_id=request.execution_id,
                provider_id=self.provider_id,
                success=False,
                error=f"scripted failure on call {self._call_count}",
                failure_class=fc,
                latency_ms=(time.perf_counter() - start) * 1000,
            )

        assignment = request.context_package.get("your_assignment") or request.prompt[:200]
        role = request.role.value
        complete = bool(self.behavior.get("always_complete", True))

        if "broken" in (request.prompt + assignment).lower() and self.behavior.get("produce_bad_code"):
            output = self._bad_code_handoff(request, assignment)
            success = True
        elif complete:
            output = self._complete_handoff(request, assignment, role)
            success = True
        else:
            output = self._partial_handoff(request, assignment, role)
            success = True

        handoff = self.parse_handoff(request.task_id, self.provider_id, output, requested=assignment)
        # write a small artifact into workspace if present
        artifacts: list[str] = []
        if request.workspace_path:
            try:
                from pathlib import Path

                ws = Path(request.workspace_path)
                ws.mkdir(parents=True, exist_ok=True)
                note = ws / "AGENT_NOTES.md"
                note.write_text(
                    f"# Agent notes ({self.provider_id})\n\nRole: {role}\n\nAssignment: {assignment}\n",
                    encoding="utf-8",
                )
                artifacts.append(str(note))
                # simple implementation artifact for coding tasks
                if "code" in role or "implement" in role or "coding" in str(request.context_package):
                    src = ws / "src"
                    src.mkdir(exist_ok=True)
                    app = src / "app.py"
                    if self.behavior.get("produce_bad_code"):
                        app.write_text("def main(\n    print('broken'\n", encoding="utf-8")
                    else:
                        app.write_text(
                            '"""Generated by AETHER mock adapter."""\n\n'
                            "def main() -> None:\n"
                            f'    print("AETHER mock implementation for {request.task_id}")\n\n'
                            "if __name__ == '__main__':\n"
                            "    main()\n",
                            encoding="utf-8",
                        )
                    artifacts.append(str(app))
            except OSError:
                pass

        self._record_request(tokens=len(output) // 4)
        self._active.pop(request.execution_id, None)
        return ExecutionResult(
            execution_id=request.execution_id,
            provider_id=self.provider_id,
            success=success,
            output=output,
            handoff=handoff,
            latency_ms=(time.perf_counter() - start) * 1000,
            artifacts=artifacts,
        )

    async def stream(self, request: ExecutionRequest) -> AsyncIterator[StreamChunk]:
        """Stream mock output in small chunks for live terminal/UI."""
        # Chat-style prompts: short natural reply streamed
        if "AETHER CHAT SESSION" in (request.prompt or "") or request.task_id.startswith("CHAT-"):
            # pull last USER: line
            user_line = "hello"
            if "USER:" in request.prompt:
                user_line = request.prompt.split("USER:")[-1].strip().splitlines()[0][:120]
            text = (
                f"[{self.provider_id}] Got it — continuing this session.\n\n"
                f"You said: {user_line}\n\n"
                "I am the mock agent. In production this stream is your live model tokens."
            )
            step = max(8, len(text) // 12)
            for i in range(0, len(text), step):
                await asyncio.sleep(0.02)
                yield StreamChunk(
                    execution_id=request.execution_id,
                    text=text[i : i + step],
                    done=False,
                )
            yield StreamChunk(execution_id=request.execution_id, text="", done=True)
            return

        result = await self.execute(request)
        text = result.output or result.error or ""
        step = max(24, len(text) // 16) if text else 1
        for i in range(0, len(text), step):
            await asyncio.sleep(0.01)
            yield StreamChunk(
                execution_id=request.execution_id,
                text=text[i : i + step],
                done=False,
            )
        yield StreamChunk(
            execution_id=request.execution_id,
            text="",
            done=True,
            metadata={"success": result.success, "error": result.error or ""},
        )

    def _complete_handoff(self, request: ExecutionRequest, assignment: str, role: str) -> str:
        return f"""STATUS: COMPLETE

COMPLETED:
- Fulfilled assignment as {role}: {assignment[:180]}
- Updated shared workspace notes
- Prepared artifacts for verification

REMAINING:
- none

FILES:
- AGENT_NOTES.md
- src/app.py

TESTS:
- smoke checks conceptual

TEST_RESULTS:
passed (mock)

KNOWN_BUGS:
- none

ASSUMPTIONS:
- Mock adapter is not a real model provider

RECOMMENDED_NEXT_ACTION:
Verify artifacts and mark subtask complete
"""

    def _partial_handoff(self, request: ExecutionRequest, assignment: str, role: str) -> str:
        return f"""STATUS: PARTIAL

COMPLETED:
- Started work as {role}
- Partial progress on: {assignment[:180]}

REMAINING:
- Finish remaining implementation
- Run tests

FILES:
- AGENT_NOTES.md

TESTS:
- none yet

KNOWN_BUGS:
- incomplete implementation

ASSUMPTIONS:
- Continuation expected via handoff

RECOMMENDED_NEXT_ACTION:
Continue implementation and add tests
"""

    def _bad_code_handoff(self, request: ExecutionRequest, assignment: str) -> str:
        return f"""STATUS: COMPLETE

COMPLETED:
- Wrote code for: {assignment[:180]}

REMAINING:
- none

FILES:
- src/app.py

TESTS:
- not run

KNOWN_BUGS:
- none claimed

ASSUMPTIONS:
- Code is correct (it is not — for verification tests)

RECOMMENDED_NEXT_ACTION:
Verify
"""
