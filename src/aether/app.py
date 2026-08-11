"""Composition root — wires AETHER components."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from aether.adapters.registry import AgentRegistry
from aether.config.settings import AetherSettings, load_settings
from aether.core.approvals import ApprovalManager
from aether.core.budget import BudgetManager
from aether.core.checkpoint import CheckpointManager
from aether.core.chat_session import ChatRouter, ChatSessionStore
from aether.core.circuit import CircuitBreaker
from aether.core.context import ContextManager
from aether.core.debate_engine import DebateEngine
from aether.core.event_bus import EventBus
from aether.core.export import TaskExporter
from aether.core.handoff import HandoffManager
from aether.core.metrics import MetricsCollector
from aether.core.orchestrator import Orchestrator
from aether.core.quota import QuotaManager
from aether.core.router import AgentRouter
from aether.core.state_store import JsonStateStore
from aether.core.task_manager import TaskManager
from aether.core.verification import VerificationEngine
from aether.core.workflow import WorkflowEngine
from aether.core.workspace import WorkspaceManager
from aether.observability.logging import set_redactor, setup_logging
from aether.security.redaction import SecretRedactor
from aether.security.sandbox import ExecutionSandbox
from aether.storage.db import Database
from aether.storage.repository import FileAuditLogger, SqliteRepository


class AetherApp:
    def __init__(self, settings: AetherSettings) -> None:
        self.settings = settings
        self.redactor = SecretRedactor(settings.security.secret_patterns or None)
        set_redactor(self.redactor)
        setup_logging(settings.logging.level, settings.logging.json_logs)

        self.db = Database(settings.database_url)
        self.repo = SqliteRepository(self.db, redactor=self.redactor)
        self.audit = FileAuditLogger(settings.data_dir / "audit" / "audit.jsonl", redactor=self.redactor)
        self.sandbox = ExecutionSandbox(
            settings.security,
            settings.orchestrator.default_approval_level,
            workspace_root=settings.workspace_root,
        )

        # Control-plane state the CLI and the server must both see. Without
        # these files, anything raised in one process is invisible to the other.
        state_dir = settings.data_dir / "state"
        self.events = EventBus()
        self.approvals = ApprovalManager(
            auto_grant_low_risk=False,
            store=JsonStateStore(state_dir / "approvals.json"),
        )
        self.budgets = BudgetManager(
            default_max_tokens=settings.budget.default_max_tokens,
            default_max_requests=settings.budget.default_max_requests,
            store=JsonStateStore(state_dir / "budgets.json"),
        )
        self.metrics = MetricsCollector(store=JsonStateStore(state_dir / "metrics.json"))
        self.workspace_mgr = WorkspaceManager(
            enable_git=settings.orchestrator.enable_git_checkpoints
        )
        self.exporter = TaskExporter(self.repo)

        self.registry = AgentRegistry(settings, sandbox=self.sandbox, include_test_adapters=True)
        self.quota = QuotaManager(
            settings, self.repo, store=JsonStateStore(state_dir / "usage.json")
        )
        self.circuit = CircuitBreaker(settings.circuit_breaker)
        self.tasks = TaskManager(settings, self.repo)
        self.router = AgentRouter(settings, self.registry, self.quota, self.circuit)
        self.handoffs = HandoffManager(self.repo)
        self.context = ContextManager(self.repo)
        self.checkpoints = CheckpointManager(settings, self.repo)
        self.verification = VerificationEngine(
            self.repo,
            sandbox=self.sandbox,
            sandbox_mode=settings.security.verification_sandbox,
        )
        self.workflow = WorkflowEngine()
        self.chat_store = ChatSessionStore(settings.data_dir / "chat_sessions")
        self.chat_router = ChatRouter(
            self,
            self.chat_store,
            compaction_ratio=settings.orchestrator.context_compaction_ratio,
            prefer_llm_summary=settings.orchestrator.prefer_llm_session_summary,
        )
        self.debate_engine = DebateEngine(self, self.chat_router)

        self.orchestrator = Orchestrator(
            settings=settings,
            repo=self.repo,
            registry=self.registry,
            task_manager=self.tasks,
            router=self.router,
            handoff_manager=self.handoffs,
            context_manager=self.context,
            checkpoint_manager=self.checkpoints,
            verification=self.verification,
            workflow=self.workflow,
            quota=self.quota,
            circuit=self.circuit,
            audit=self.audit,
            event_bus=self.events,
            approvals=self.approvals,
            budgets=self.budgets,
            metrics=self.metrics,
            workspace_manager=self.workspace_mgr,
        )
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self.settings.ensure_dirs()
        await self.db.create_all()
        await self.registry.initialize_all()
        await self.orchestrator.recover_running()
        self._started = True

    async def stop(self) -> None:
        await self.registry.shutdown_all()
        await self.db.dispose()
        self._started = False


_app: Optional[AetherApp] = None


def get_app(config_path: Optional[str | Path] = None, force_reload: bool = False) -> AetherApp:
    global _app
    if _app is None or force_reload:
        settings = load_settings(config_path)
        _app = AetherApp(settings)
    return _app


async def create_app(config_path: Optional[str | Path] = None) -> AetherApp:
    app = get_app(config_path, force_reload=True)
    await app.start()
    return app
