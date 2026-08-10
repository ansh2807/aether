"""Regressions for the orchestrator correctness fixes.

Covers:
  * testing ("mock") providers must not absorb work real providers failed at
  * agent-branch merges must really merge, and must surface conflicts
  * cancelling a task must kill in-flight adapter executions
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from aether.adapters.base import BaseAdapter
from aether.adapters.registry import AgentRegistry
from aether.config.settings import AetherSettings
from aether.core.circuit import CircuitBreaker
from aether.core.quota import QuotaManager
from aether.core.router import AgentRouter
from aether.core.workspace import WorkspaceManager
from aether.domain.enums import AdapterType, AgentRole
from aether.domain.models import ExecutionRequest, ProviderConfig, Task

needs_git = pytest.mark.skipif(not shutil.which("git"), reason="git not installed")


def _router_for(settings: AetherSettings) -> AgentRouter:
    registry = AgentRegistry(settings)
    registry.build_from_config()
    return AgentRouter(settings, registry, QuotaManager(settings), CircuitBreaker(settings.circuit_breaker))


def _settings_with(providers: dict[str, ProviderConfig], tmp_path: Path) -> AetherSettings:
    s = AetherSettings(data_dir=tmp_path / "data", workspace_root=tmp_path / "workspace")
    s.providers = providers
    return s


def _mock_cfg(enabled: bool = True) -> ProviderConfig:
    return ProviderConfig(
        provider_id="mock",
        display_name="Mock",
        adapter=AdapterType.MOCK,
        enabled=enabled,
        for_testing=True,
        capabilities={"coding": 8, "reasoning": 8, "architecture": 8},
    )


def _real_cfg(provider_id: str = "openai") -> ProviderConfig:
    return ProviderConfig(
        provider_id=provider_id,
        display_name="Real",
        adapter=AdapterType.API,
        enabled=True,
        base_url="http://127.0.0.1:9/v1",
        model="x",
        api_key_env="AETHER_TEST_KEY",
        capabilities={"coding": 9, "reasoning": 9, "architecture": 9},
    )


# --- #1 mock must not be a failover target -------------------------------


@pytest.mark.asyncio
async def test_mock_not_routable_when_real_provider_configured(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AETHER_TEST_KEY", "sk-test")
    settings = _settings_with({"mock": _mock_cfg(), "openai": _real_cfg()}, tmp_path)
    router = _router_for(settings)
    task = Task(original_prompt="build something")

    # the real provider is unreachable, so it is excluded — mock must NOT take over
    decision = await router.select(task, role=AgentRole.IMPLEMENTER, exclude=["openai"])
    assert decision is None


@pytest.mark.asyncio
async def test_mock_routable_when_it_is_the_only_provider(tmp_path: Path):
    settings = _settings_with({"mock": _mock_cfg()}, tmp_path)
    router = _router_for(settings)
    decision = await router.select(Task(original_prompt="build something"), role=AgentRole.IMPLEMENTER)
    assert decision is not None and decision.provider_id == "mock"


@pytest.mark.asyncio
async def test_testing_fallback_can_be_opted_into(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AETHER_TEST_KEY", "sk-test")
    settings = _settings_with({"mock": _mock_cfg(), "openai": _real_cfg()}, tmp_path)
    settings.routing.allow_testing_fallback = True
    router = _router_for(settings)
    decision = await router.select(
        Task(original_prompt="build something"), role=AgentRole.IMPLEMENTER, exclude=["openai"]
    )
    assert decision is not None and decision.provider_id == "mock"


@pytest.mark.asyncio
async def test_chat_router_excludes_mock_when_real_provider_exists(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AETHER_TEST_KEY", "sk-test")
    from aether.core.chat_session import ChatRouter, ChatSessionStore

    settings = _settings_with({"mock": _mock_cfg(), "openai": _real_cfg()}, tmp_path)
    registry = AgentRegistry(settings)
    registry.build_from_config()

    class _App:
        pass

    app = _App()
    app.registry = registry
    app.settings = settings
    router = ChatRouter(app, ChatSessionStore(tmp_path / "sessions"))
    assert router._enabled_providers() == ["openai"]


# --- #2 agent branch merges ----------------------------------------------


@needs_git
@pytest.mark.asyncio
async def test_merge_detects_conflict_between_parallel_agents(tmp_path: Path):
    ws = tmp_path / "ws"
    mgr = WorkspaceManager(enable_git=True)
    await mgr.ensure_repo(ws)
    (ws / "shared.txt").write_text("base\n", encoding="utf-8")
    await mgr._run(ws, "add", "-A")
    await mgr._run(ws, "commit", "-m", "base")
    main = await mgr.base_branch(ws)
    assert main is not None

    # agent A branches from main and edits the shared file
    branch_a = await mgr.checkout_agent_branch(ws, "agent_a", "sub1")
    (ws / "shared.txt").write_text("A\n", encoding="utf-8")
    await mgr.commit_agent_work(ws, "agent_a", "A work")

    # agent B branches from the same base (parallel), edits the same file
    await mgr._run(ws, "checkout", main)
    branch_b = await mgr.checkout_agent_branch(ws, "agent_b", "sub2")
    (ws / "shared.txt").write_text("B\n", encoding="utf-8")
    await mgr.commit_agent_work(ws, "agent_b", "B work")

    ok_a, conflicts_a, _ = await mgr.merge_to_main(ws, branch_a)
    assert ok_a and not conflicts_a

    ok_b, conflicts_b, _ = await mgr.merge_to_main(ws, branch_b)
    assert not ok_b, "second agent's conflicting change was silently accepted"
    assert "shared.txt" in conflicts_b

    # main kept agent A's work rather than being reset to B's branch tip
    _, head = await mgr._run(ws, "rev-parse", "--abbrev-ref", "HEAD")
    assert head.strip() == main
    assert (ws / "shared.txt").read_text(encoding="utf-8").strip() == "A"


@needs_git
@pytest.mark.asyncio
async def test_merge_preserves_existing_main_commits(tmp_path: Path):
    ws = tmp_path / "ws"
    mgr = WorkspaceManager(enable_git=True)
    await mgr.ensure_repo(ws)
    (ws / "a.txt").write_text("1\n", encoding="utf-8")
    await mgr._run(ws, "add", "-A")
    await mgr._run(ws, "commit", "-m", "first")
    main = await mgr.base_branch(ws)

    branch = await mgr.checkout_agent_branch(ws, "agent", "sub")
    (ws / "b.txt").write_text("2\n", encoding="utf-8")
    await mgr.commit_agent_work(ws, "agent", "adds b")
    ok, _, _ = await mgr.merge_to_main(ws, branch)
    assert ok

    # both files survive the merge
    assert (ws / "a.txt").exists() and (ws / "b.txt").exists()
    _, log = await mgr._run(ws, "log", "--oneline", main)
    assert "first" in log


# --- #4 cancel kills running executions -----------------------------------


class _HangingAdapter(BaseAdapter):
    """Spawns a real long-lived child so cancellation is observable."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self._procs: dict[str, asyncio.subprocess.Process] = {}

    async def execute(self, request: ExecutionRequest):  # pragma: no cover - not used
        raise NotImplementedError

    async def start(self, execution_id: str) -> asyncio.subprocess.Process:
        import sys

        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(30)"
        )
        self._procs[execution_id] = proc
        self._active[execution_id] = True
        return proc

    async def cancel(self, execution_id: str) -> None:
        proc = self._procs.get(execution_id)
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()
        await super().cancel(execution_id)

    async def cancel_all(self) -> None:
        for execution_id in list(self._procs.keys()):
            await self.cancel(execution_id)
        await super().cancel_all()


@pytest.mark.asyncio
async def test_cancel_all_kills_every_in_flight_execution():
    cfg = ProviderConfig(provider_id="hang", display_name="Hang", adapter=AdapterType.CLI)
    adapter = _HangingAdapter(cfg)
    procs = [await adapter.start(f"ex-{i}") for i in range(3)]
    assert all(p.returncode is None for p in procs)

    await adapter.cancel_all()

    assert all(p.returncode is not None for p in procs)
    assert adapter._active == {}


@pytest.mark.asyncio
async def test_base_cancel_all_clears_tracked_executions():
    adapter = BaseAdapter(ProviderConfig(provider_id="p", display_name="P"))
    adapter._active = {"a": True, "b": True}
    await adapter.cancel_all()
    assert adapter._active == {}
