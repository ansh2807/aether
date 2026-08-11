"""Configuration loading for AETHER."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from aether.domain.enums import AdapterType, ApprovalLevel, TaskMode
from aether.domain.models import ProviderConfig


def _default_root() -> Path:
    env = os.environ.get("AETHER_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path.cwd() / "data"


class ServerSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 7420
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    # The API drives agents, spawns subprocesses and stores provider keys, and
    # it has no user model — so it is token-gated by default. The token is
    # generated into data/api_token on first serve.
    require_auth: bool = True
    api_token: str = ""
    # Built dashboard to serve at "/". Empty = auto-detect <repo>/web/dist.
    # Serving it here means no Vite dev server is needed to use the UI.
    static_dir: str = ""


class OrchestratorSettings(BaseModel):
    max_agent_retries: int = 3
    max_verification_loops: int = 3
    default_approval_level: ApprovalLevel = ApprovalLevel.SAFE
    default_mode: TaskMode = TaskMode.SOLO
    agent_timeout_seconds: int = 600
    network_retries: int = 2
    debate_max_rounds: int = 3
    debate_max_agents: int = 4
    max_concurrent_subtasks: int = 4
    enable_git_checkpoints: bool = True
    enable_agent_branches: bool = True
    approval_timeout_seconds: int = 0  # 0 = queue without blocking wait
    require_approval_on_start: bool = False
    # Chat / LLM-router session behaviour
    context_compaction_ratio: float = 0.78  # summarize+rollover when usage ≥ this
    prefer_llm_session_summary: bool = True
    chat_max_failovers: int = 4
    default_chat_provider: Optional[str] = None
    # Ask a model to plan the subtask DAG. Falls back to the deterministic
    # keyword planner when no provider is available or the plan fails schema
    # validation - the orchestrator still owns execution either way.
    use_llm_planner: bool = True
    planner_max_subtasks: int = 8


class BudgetSettings(BaseModel):
    default_max_tokens: Optional[int] = 500_000
    default_max_requests: Optional[int] = 200
    default_max_cost_usd: Optional[float] = None
    usd_per_1k_tokens: float = 0.002


class CircuitBreakerSettings(BaseModel):
    failure_threshold: int = 5
    recovery_timeout_seconds: int = 60
    half_open_max_calls: int = 1


class QuotaSettings(BaseModel):
    default_requests_per_minute: int = 30
    default_daily_requests: int = 1000


class SecuritySettings(BaseModel):
    approval_level: ApprovalLevel = ApprovalLevel.SAFE
    secret_patterns: list[str] = Field(default_factory=list)
    command_denylist: list[str] = Field(default_factory=list)
    command_allowlist_prefixes: list[str] = Field(default_factory=list)
    max_subprocess_output_bytes: int = 2_000_000
    workspace_only: bool = True
    # How to run verification commands over agent-written code:
    #   subprocess - scrubbed environment, no inherited secrets (default)
    #   container  - docker/podman, no network, resource capped
    #   off        - inherit everything; only for a workspace you trust
    verification_sandbox: str = "subprocess"


class LoggingSettings(BaseModel):
    level: str = "INFO"
    json_logs: bool = True


class RoutingSettings(BaseModel):
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "capability": 0.45,
            "availability": 0.25,
            "health": 0.15,
            "cost": 0.05,
            "latency": 0.05,
            "failure_history": 0.05,
        }
    )
    prefer_different_agent_on_failover: bool = True
    user_preferences: list[str] = Field(default_factory=list)
    # `for_testing` providers (the mock) fabricate plausible output and always
    # report success. They must never silently absorb work that a real provider
    # failed at. Only routable when no real provider is configured, unless the
    # operator explicitly opts in.
    allow_testing_fallback: bool = False


class AetherSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AETHER_", extra="ignore")

    data_dir: Path = Field(default_factory=lambda: Path("./data"))
    workspace_root: Path = Field(default_factory=lambda: Path("./workspace"))
    database_url: str = "sqlite+aiosqlite:///./data/aether.db"
    server: ServerSettings = Field(default_factory=ServerSettings)
    orchestrator: OrchestratorSettings = Field(default_factory=OrchestratorSettings)
    circuit_breaker: CircuitBreakerSettings = Field(default_factory=CircuitBreakerSettings)
    quota: QuotaSettings = Field(default_factory=QuotaSettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    routing: RoutingSettings = Field(default_factory=RoutingSettings)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "logs").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "audit").mkdir(parents=True, exist_ok=True)


def _parse_provider(provider_id: str, raw: dict[str, Any]) -> ProviderConfig:
    adapter_raw = str(raw.get("adapter", "unavailable")).lower()
    try:
        adapter = AdapterType(adapter_raw)
    except ValueError:
        adapter = AdapterType.UNAVAILABLE
    limits = dict(raw.get("limits") or {})
    ctx = raw.get("context_window")
    if ctx is None and limits.get("context_window") is not None:
        ctx = limits.get("context_window")
    try:
        context_window = int(ctx) if ctx is not None else None
    except (TypeError, ValueError):
        context_window = None

    return ProviderConfig(
        provider_id=provider_id,
        display_name=raw.get("display_name", provider_id),
        adapter=adapter,
        enabled=bool(raw.get("enabled", False)),
        for_testing=bool(raw.get("for_testing", False)),
        command=str(raw.get("command", "") or ""),
        args=list(raw.get("args") or []),
        base_url=str(raw.get("base_url", "") or ""),
        model=str(raw.get("model", "") or ""),
        api_key_env=str(raw.get("api_key_env", "") or ""),
        capabilities=dict(raw.get("capabilities") or {}),
        roles_preferred=list(raw.get("roles_preferred") or []),
        limits=limits,
        context_window=context_window,
        status_note=str(raw.get("status_note", "") or ""),
        extra={k: v for k, v in raw.items() if k not in {
            "display_name", "adapter", "enabled", "for_testing", "command", "args",
            "base_url", "model", "api_key_env", "capabilities", "roles_preferred",
            "limits", "status_note", "context_window",
        }},
    )


def load_yaml_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config root must be a mapping: {path}")
    return data


def find_config_path(explicit: Optional[str | Path] = None) -> Optional[Path]:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if p.exists():
            return p
        raise FileNotFoundError(f"Config not found: {p}")

    env = os.environ.get("AETHER_CONFIG")
    if env:
        p = Path(env).expanduser().resolve()
        if p.exists():
            return p

    candidates = [
        Path.cwd() / "aether.yaml",
        Path.cwd() / "config" / "aether.yaml",
        Path.cwd() / "config" / "aether.default.yaml",
        Path.home() / ".aether" / "config.yaml",
    ]
    # package-relative default shipped with repo
    here = Path(__file__).resolve()
    repo_default = here.parents[3] / "config" / "aether.default.yaml"
    candidates.append(repo_default)

    for c in candidates:
        if c.exists():
            return c
    return None


def settings_from_dict(data: dict[str, Any]) -> AetherSettings:
    providers_raw = data.get("providers") or {}
    providers: dict[str, ProviderConfig] = {}
    for pid, raw in providers_raw.items():
        if isinstance(raw, dict):
            providers[pid] = _parse_provider(pid, raw)

    payload = {k: v for k, v in data.items() if k != "providers"}
    # nested models
    if "logging" in payload and isinstance(payload["logging"], dict):
        log_raw = dict(payload["logging"])
        if "json" in log_raw and "json_logs" not in log_raw:
            log_raw["json_logs"] = log_raw.pop("json")
        payload["logging"] = LoggingSettings(**log_raw)

    for key, cls in [
        ("server", ServerSettings),
        ("orchestrator", OrchestratorSettings),
        ("circuit_breaker", CircuitBreakerSettings),
        ("quota", QuotaSettings),
        ("budget", BudgetSettings),
        ("security", SecuritySettings),
        ("routing", RoutingSettings),
    ]:
        if key in payload and isinstance(payload[key], dict):
            payload[key] = cls(**payload[key])

    if "data_dir" in payload:
        payload["data_dir"] = Path(payload["data_dir"])
    if "workspace_root" in payload:
        payload["workspace_root"] = Path(payload["workspace_root"])

    settings = AetherSettings(**payload, providers=providers)
    return settings


def load_settings(config_path: Optional[str | Path] = None) -> AetherSettings:
    path = find_config_path(config_path)
    if path is None:
        settings = AetherSettings()
        # enable mock by default when no config found
        settings.providers["mock"] = ProviderConfig(
            provider_id="mock",
            display_name="Mock Agent",
            adapter=AdapterType.MOCK,
            enabled=True,
            for_testing=True,
            capabilities={
                "coding": 8,
                "reasoning": 8,
                "research": 7,
                "testing": 8,
                "terminal": 8,
                "review": 7,
                "architecture": 7,
            },
            roles_preferred=["architect", "implementer", "tester", "reviewer", "debugger"],
        )
        settings.ensure_dirs()
        return settings

    data = load_yaml_config(path)
    settings = settings_from_dict(data)
    # resolve relative paths against cwd
    if not settings.data_dir.is_absolute():
        settings.data_dir = (Path.cwd() / settings.data_dir).resolve()
    if not settings.workspace_root.is_absolute():
        settings.workspace_root = (Path.cwd() / settings.workspace_root).resolve()
    # fix sqlite path if relative
    if settings.database_url.startswith("sqlite") and ":///" in settings.database_url:
        prefix, db_path = settings.database_url.split(":///", 1)
        p = Path(db_path)
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
            p.parent.mkdir(parents=True, exist_ok=True)
            settings.database_url = f"{prefix}:///{p.as_posix()}"
    settings.ensure_dirs()
    return settings
