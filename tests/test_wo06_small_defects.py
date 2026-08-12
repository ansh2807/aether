"""WO-06 — four small independent defects, each with a regression test.

  1. verification.VerificationEngine._run must not orphan the child on timeout
  2. router.AgentRouter.select must cache health_check() with a short TTL
  3. OpencodeCliAdapter.execute must not put long prompts on argv (Windows ~32KB cap)
  4. ChatRouter.chat_stream must not double-execute on an empty-but-valid reply
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

import pytest

from aether.adapters.base import BaseAdapter
from aether.adapters.registry import AgentRegistry
from aether.app import AetherApp
from aether.config.settings import AetherSettings
from aether.core.circuit import CircuitBreaker
from aether.core.quota import QuotaManager
from aether.core.router import HEALTH_CACHE_TTL_SECONDS, AgentRouter
from aether.core.verification import VerificationEngine
from aether.domain.enums import AdapterType, AgentRole
from aether.domain.models import (
    ExecutionRequest,
    ExecutionResult,
    HealthStatus,
    ProviderConfig,
    Task,
    utcnow,
)


# --- #1 verification timeout must not leak the child process --------------


@pytest.mark.asyncio
async def test_verification_run_kills_process_on_timeout(tmp_path: Path, monkeypatch):
    import asyncio

    # This test exercises host-process timeout cleanup specifically; production
    # verification defaults to the fail-closed container mode.
    engine = VerificationEngine(repo=None, sandbox_mode="subprocess")  # repo unused by _run
    captured: dict[str, "asyncio.subprocess.Process"] = {}
    orig_create = asyncio.create_subprocess_exec

    async def capturing_create(*args, **kwargs):
        proc = await orig_create(*args, **kwargs)
        captured["proc"] = proc
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capturing_create)

    start = time.perf_counter()
    code, out = await engine._run(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        timeout=1,
    )
    elapsed = time.perf_counter() - start

    # returned promptly instead of waiting out the full 30s sleep
    assert elapsed < 15
    assert code == 1
    assert "timed out" in out
    # the orphan check: the child must have actually been killed, not left running
    proc = captured["proc"]
    assert proc.returncode is not None


# --- #2 router caches health_check with a short TTL ------------------------


class _CountingHealthAdapter(BaseAdapter):
    """Adapter that counts health_check() calls; always reports healthy."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self.health_check_calls = 0

    async def health_check(self) -> HealthStatus:
        self.health_check_calls += 1
        return HealthStatus(
            provider_id=self.provider_id,
            available=True,
            latency_ms=1.0,
            message="healthy",
            checked_at=utcnow(),
        )

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:  # pragma: no cover - unused
        raise NotImplementedError


def _router_with_counting_adapter(tmp_path: Path) -> tuple[AgentRouter, _CountingHealthAdapter]:
    cfg = ProviderConfig(
        provider_id="counted",
        display_name="Counted",
        adapter=AdapterType.API,
        enabled=True,
        for_testing=False,
        capabilities={"coding": 8, "reasoning": 8},
    )
    settings = AetherSettings(data_dir=tmp_path / "data", workspace_root=tmp_path / "workspace")
    settings.providers = {"counted": cfg}
    registry = AgentRegistry(settings)
    adapter = _CountingHealthAdapter(cfg)
    registry.register(adapter, cfg)
    router = AgentRouter(settings, registry, QuotaManager(settings), CircuitBreaker(settings.circuit_breaker))
    return router, adapter


@pytest.mark.asyncio
async def test_health_check_is_cached_across_selects(tmp_path: Path):
    router, adapter = _router_with_counting_adapter(tmp_path)
    task = Task(original_prompt="build something")

    for _ in range(5):
        decision = await router.select(task, role=AgentRole.IMPLEMENTER)
        assert decision is not None and decision.provider_id == "counted"

    assert adapter.health_check_calls == 1, "5 selects should probe health once, not 5 times"


@pytest.mark.asyncio
async def test_invalidate_health_forces_a_fresh_probe(tmp_path: Path):
    router, adapter = _router_with_counting_adapter(tmp_path)
    task = Task(original_prompt="build something")

    await router.select(task, role=AgentRole.IMPLEMENTER)
    assert adapter.health_check_calls == 1

    router.invalidate_health("counted")
    await router.select(task, role=AgentRole.IMPLEMENTER)
    assert adapter.health_check_calls == 2


@pytest.mark.asyncio
async def test_select_failover_invalidates_the_failed_providers_health(tmp_path: Path):
    router, adapter = _router_with_counting_adapter(tmp_path)
    task = Task(original_prompt="build something")

    await router.select(task, role=AgentRole.IMPLEMENTER)
    assert adapter.health_check_calls == 1

    # select_failover excludes "counted" itself, so no candidates remain — but
    # it must still have invalidated the cache entry for the failed provider.
    await router.select_failover(task, failed_provider="counted", role=AgentRole.IMPLEMENTER)
    assert "counted" not in router._health_cache


def test_health_cache_ttl_is_a_module_level_constant():
    assert HEALTH_CACHE_TTL_SECONDS == 30.0


# --- #3 Open Code: long prompts go via stdin/temp file, not argv -----------


class _FakeStream:
    """Minimal asyncio.StreamReader stand-in: yields its payload once, then EOF."""

    def __init__(self, payload: bytes = b"") -> None:
        self._payload = payload

    async def read(self, _n: int = -1) -> bytes:
        data, self._payload = self._payload, b""
        return data


class _FakeProc:
    """The adapter reads pipes incrementally now, not via communicate().

    These tests only care about argv, but the fake still has to look like a
    real process to `stream_process` — `.stdout`/`.stderr` readers and a `.wait`.
    `stdin` is None because Open Code attaches a real file, never a pipe.
    """

    def __init__(self) -> None:
        self.returncode: Optional[int] = 0
        self.stdout = _FakeStream(b"STATUS: COMPLETE\n\nCOMPLETED:\n- ok\n")
        self.stderr = _FakeStream(b"")
        self.stdin = None

    async def communicate(self):
        return b"STATUS: COMPLETE\n\nCOMPLETED:\n- ok\n", b""

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


@pytest.mark.asyncio
async def test_long_prompt_does_not_go_on_argv(tmp_path: Path, monkeypatch):
    from aether.adapters import opencode_adapter as oc_mod

    monkeypatch.setattr(oc_mod, "opencode_bin", lambda: "opencode-fake")

    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(oc_mod.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    cfg = ProviderConfig(
        provider_id="oc",
        display_name="Open Code",
        adapter=AdapterType.CLI,
        enabled=True,
        model="opencode/test-model",
    )
    adapter = oc_mod.OpencodeCliAdapter(cfg)

    long_prompt = "x" * (64 * 1024)  # 64KB — well past Windows' ~32KB argv cap
    req = ExecutionRequest(
        task_id="T1",
        provider_id="oc",
        prompt=long_prompt,
        workspace_path=str(tmp_path),
        timeout_seconds=30,
    )

    result = await adapter.execute(req)

    assert result.success is True
    argv = captured["args"]
    assert long_prompt not in argv
    assert all(len(str(a)) < oc_mod.PROMPT_STDIN_THRESHOLD_CHARS for a in argv)
    # the prompt was routed through stdin instead
    assert captured["kwargs"].get("stdin") is not None


@pytest.mark.asyncio
async def test_short_prompt_still_goes_on_argv(tmp_path: Path, monkeypatch):
    """Common case must not change: short prompts stay as an argv element."""
    from aether.adapters import opencode_adapter as oc_mod

    monkeypatch.setattr(oc_mod, "opencode_bin", lambda: "opencode-fake")

    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(oc_mod.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    cfg = ProviderConfig(
        provider_id="oc",
        display_name="Open Code",
        adapter=AdapterType.CLI,
        enabled=True,
        model="opencode/test-model",
    )
    adapter = oc_mod.OpencodeCliAdapter(cfg)

    short_prompt = "please write a haiku"
    req = ExecutionRequest(
        task_id="T2",
        provider_id="oc",
        prompt=short_prompt,
        workspace_path=str(tmp_path),
        timeout_seconds=30,
    )

    result = await adapter.execute(req)

    assert result.success is True
    assert short_prompt in captured["args"]
    assert captured["kwargs"].get("stdin") is None


# --- #4 chat_stream must not double-execute on an empty-but-valid reply ----


class _EmptyReplyAdapter(BaseAdapter):
    """Reports success with empty output; does NOT override stream(), so it
    goes through BaseAdapter.stream()'s default impl (which itself calls
    execute() once to produce the single chunk)."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self.execute_calls = 0

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.execute_calls += 1
        return ExecutionResult(
            execution_id=request.execution_id,
            provider_id=self.provider_id,
            success=True,
            output="",
        )


@pytest.mark.asyncio
async def test_chat_stream_does_not_double_execute_on_empty_success(app: AetherApp):
    cfg = ProviderConfig(
        provider_id="empty_ok",
        display_name="Empty OK",
        adapter=AdapterType.MOCK,
        enabled=True,
        for_testing=True,
        capabilities={"reasoning": 8},
    )
    adapter = _EmptyReplyAdapter(cfg)
    app.registry.register(adapter, cfg)

    session = app.chat_router.create_session(provider_id="empty_ok", mode="chat")
    assert session.active_provider == "empty_ok"

    events = []
    async for ev in app.chat_router.chat_stream(session, "Say nothing useful"):
        events.append(ev)

    assert adapter.execute_calls <= 1, "empty-but-successful reply must not be re-executed"

    done = next(e for e in events if e.get("type") == "done")
    assert done["ok"] is True
    assert done["reply"] == ""
