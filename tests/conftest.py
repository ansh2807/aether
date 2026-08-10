"""Pytest fixtures for AETHER."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from aether.adapters.mock_adapter import MockAdapter
from aether.adapters.registry import AgentRegistry
from aether.app import AetherApp
from aether.config.settings import (
    AetherSettings,
    CircuitBreakerSettings,
    OrchestratorSettings,
    QuotaSettings,
    SecuritySettings,
)
from aether.domain.enums import AdapterType
from aether.domain.models import ProviderConfig


@pytest.fixture
def tmp_aether_dir(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    ws = tmp_path / "workspace"
    data.mkdir()
    ws.mkdir()
    return tmp_path


@pytest.fixture
def settings(tmp_aether_dir: Path) -> AetherSettings:
    db = tmp_aether_dir / "data" / "test.db"
    s = AetherSettings(
        data_dir=tmp_aether_dir / "data",
        workspace_root=tmp_aether_dir / "workspace",
        database_url=f"sqlite+aiosqlite:///{db.as_posix()}",
        orchestrator=OrchestratorSettings(
            max_agent_retries=2,
            max_verification_loops=2,
            agent_timeout_seconds=30,
            enable_git_checkpoints=False,
        ),
        circuit_breaker=CircuitBreakerSettings(failure_threshold=3, recovery_timeout_seconds=1),
        quota=QuotaSettings(default_requests_per_minute=1000, default_daily_requests=10000),
        security=SecuritySettings(approval_level="safe"),
    )
    s.providers = {
        "mock": ProviderConfig(
            provider_id="mock",
            display_name="Mock",
            adapter=AdapterType.MOCK,
            enabled=True,
            for_testing=True,
            capabilities={
                "coding": 9,
                "reasoning": 9,
                "research": 8,
                "testing": 9,
                "terminal": 8,
                "review": 8,
                "architecture": 8,
            },
            roles_preferred=[
                "architect",
                "implementer",
                "tester",
                "reviewer",
                "debugger",
                "security_reviewer",
                "final_reviewer",
                "researcher",
                "general",
            ],
        ),
        "mock_b": ProviderConfig(
            provider_id="mock_b",
            display_name="Mock B",
            adapter=AdapterType.MOCK,
            enabled=True,
            for_testing=True,
            capabilities={
                "coding": 8,
                "reasoning": 8,
                "research": 7,
                "testing": 8,
                "terminal": 7,
                "review": 7,
                "architecture": 7,
            },
            roles_preferred=["implementer", "debugger", "tester"],
        ),
    }
    s.ensure_dirs()
    return s


@pytest_asyncio.fixture
async def app(settings: AetherSettings) -> AetherApp:
    application = AetherApp(settings)
    await application.start()
    yield application
    await application.stop()


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
