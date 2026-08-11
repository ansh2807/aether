"""User session / profile for interactive onboarding.

Stores non-secret preferences in data/user_profile.yaml.
Stores API keys in data/.env (never committed; gitignored).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import yaml
from pydantic import BaseModel, Field

from aether.domain.enums import TaskMode

if TYPE_CHECKING:  # avoids a cycle: providers is imported lazily below
    from aether.config.providers import MarketProvider


class UserProfile(BaseModel):
    """Last-used interactive preferences."""

    mode: TaskMode = TaskMode.SOLO
    # primary platforms selected at login (claude, google, grok, kimi, opencode, …)
    selected_providers: list[str] = Field(default_factory=list)
    # provider_id -> model name
    models: dict[str, str] = Field(default_factory=dict)
    # Open Code multi-model picks (full opencode/model ids) → virtual providers
    opencode_models: list[str] = Field(default_factory=list)
    prefer_cli: bool = True
    setup_complete: bool = False
    last_prompt: str = ""
    multi_roles: dict[str, str] = Field(default_factory=dict)


def profile_path(data_dir: Path) -> Path:
    return data_dir / "user_profile.yaml"


def env_path(data_dir: Path) -> Path:
    return data_dir / ".env"


def load_profile(data_dir: Path) -> UserProfile:
    path = profile_path(data_dir)
    if not path.exists():
        return UserProfile()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return UserProfile.model_validate(raw)
    except Exception:  # noqa: BLE001
        return UserProfile()


def save_profile(data_dir: Path, profile: UserProfile) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = profile_path(data_dir)
    path.write_text(
        yaml.safe_dump(profile.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    return path


def load_dotenv_file(path: Path) -> dict[str, str]:
    """Load KEY=VALUE lines into os.environ (does not override existing)."""
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if not key:
            continue
        loaded[key] = val
        if key not in os.environ or not os.environ.get(key):
            os.environ[key] = val
    return loaded


def set_env_key(path: Path, key: str, value: str) -> None:
    """Upsert a key in .env file and process env."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    found = False
    new_lines: list[str] = []
    for line in lines:
        if line.strip().startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        k, _, _ = line.partition("=")
        if k.strip() == key:
            new_lines.append(f"{key}={value}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    os.environ[key] = value


def safe_provider_id(model_id: str) -> str:
    """opencode/kimi-k2.7-code → opencode_kimi_k2_7_code"""
    raw = model_id.replace("/", "_").replace(".", "_").replace("-", "_")
    raw = "".join(c if c.isalnum() or c == "_" else "_" for c in raw)
    return f"oc_{raw}"[:64]


def apply_profile_to_settings_dict(base: dict[str, Any], profile: UserProfile) -> dict[str, Any]:
    """Return a config dict with selected providers enabled and models set."""
    data = dict(base)
    providers = dict(data.get("providers") or {})
    selected = set(profile.selected_providers)

    for pid, raw in list(providers.items()):
        if not isinstance(raw, dict):
            continue
        p = dict(raw)
        if pid in selected:
            p["enabled"] = True
            if pid in profile.models and profile.models[pid]:
                p["model"] = profile.models[pid]
        elif pid == "mock" and not selected:
            p["enabled"] = True
        elif pid == "mock" and selected and "mock" not in selected:
            p["enabled"] = False
            p["for_testing"] = True
        else:
            if pid not in selected and not str(pid).startswith("oc_"):
                p["enabled"] = False
        providers[pid] = p

    if selected and "mock" not in selected and "mock" in providers:
        providers["mock"]["enabled"] = False

    # Virtual providers for each Open Code model pick (multi-model)
    for model_id in profile.opencode_models:
        vid = safe_provider_id(model_id)
        short = model_id.split("/")[-1]
        providers[vid] = {
            "display_name": f"Open Code · {short}",
            "adapter": "cli",
            "enabled": True,
            "command": "opencode",
            "model": model_id,
            "adapter_class": "aether.adapters.opencode_adapter:OpencodeCliAdapter",
            "capabilities": {
                "coding": 9,
                "reasoning": 8,
                "research": 7,
                "testing": 8,
                "terminal": 9,
                "review": 7,
                "architecture": 7,
            },
            "roles_preferred": [
                "implementer",
                "architect",
                "tester",
                "debugger",
                "reviewer",
                "researcher",
            ],
            "for_testing": False,
            "extra": {"model_id": model_id, "via": "opencode"},
        }
        selected.add(vid)

    # Also keep base opencode_cli enabled if any opencode models or opencode selected
    if profile.opencode_models or "opencode_cli" in profile.selected_providers:
        if "opencode_cli" in providers:
            providers["opencode_cli"] = dict(providers["opencode_cli"])
            providers["opencode_cli"]["enabled"] = True
            if profile.opencode_models:
                providers["opencode_cli"]["model"] = profile.opencode_models[0]
            elif profile.models.get("opencode_cli"):
                providers["opencode_cli"]["model"] = profile.models["opencode_cli"]

    data["providers"] = providers
    orch = dict(data.get("orchestrator") or {})
    orch["default_mode"] = profile.mode.value
    data["orchestrator"] = orch

    routing = dict(data.get("routing") or {})
    prefs = [f"prefer:{p}" for p in profile.selected_providers]
    for m in profile.opencode_models:
        prefs.append(f"prefer:{safe_provider_id(m)}")
    routing["user_preferences"] = prefs
    data["routing"] = routing
    return data


# Primary lineup shown at login (user request: grok, kimi, google, open code, claude)
PROVIDER_CATALOG: list[dict[str, Any]] = [
    {
        "id": "claude_cli",
        "name": "Claude",
        "family": "claude",
        "kind": "cli",
        "connect": "cli_oauth",
        "command": "claude",
        "models": [
            "claude (CLI default)",
            "claude-opus",
            "claude-sonnet",
            "claude-haiku",
        ],
        "default_model": "claude (CLI default)",
        "blurb": "Browser OAuth via claude auth login (Claude Code style)",
    },
    {
        "id": "gemini",
        "name": "Google (Gemini)",
        "family": "google",
        "kind": "api",
        "connect": "browser_key",
        "api_key_env": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_url": "https://aistudio.google.com/apikey",
        "models": [
            "gemini-2.0-flash",
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-1.5-pro",
            "gemini-1.5-flash",
        ],
        "default_model": "gemini-2.0-flash",
        "blurb": "Google AI Studio API key",
    },
    {
        "id": "grok",
        "name": "Grok (xAI)",
        "family": "grok",
        "kind": "api",
        "connect": "browser_key",
        "api_key_env": "XAI_API_KEY",
        "base_url": "https://api.x.ai/v1",
        "key_url": "https://console.x.ai/",
        "models": [
            "grok-2-latest",
            "grok-3",
            "grok-3-mini",
            "grok-2-vision-1212",
        ],
        "default_model": "grok-2-latest",
        "blurb": "xAI console API key",
    },
    {
        "id": "kimi",
        "name": "Kimi (Moonshot)",
        "family": "kimi",
        "kind": "api",
        "connect": "browser_key",
        "api_key_env": "KIMI_API_KEY",
        "base_url": "https://api.moonshot.cn/v1",
        "key_url": "https://platform.moonshot.cn/console/api-keys",
        "models": [
            "moonshot-v1-auto",
            "moonshot-v1-128k",
            "moonshot-v1-32k",
            "moonshot-v1-8k",
            "kimi-latest",
        ],
        "default_model": "moonshot-v1-auto",
        "blurb": "Moonshot/Kimi API key (OpenAI-compatible)",
    },
    {
        "id": "opencode_cli",
        "name": "Open Code (multi-model hub)",
        "family": "opencode",
        "kind": "cli",
        "connect": "cli_oauth",
        "command": "opencode",
        "models": [],  # filled live from `opencode models`
        "default_model": "opencode/claude-sonnet-4-5",
        "blurb": "Open Code CLI — pick many models (Kimi, Grok, Claude, GPT, Gemini, …)",
        "multi_model": True,
    },
    {
        "id": "openai",
        "name": "OpenAI (GPT / Codex API)",
        "family": "openai",
        "kind": "api",
        "connect": "browser_key",
        "api_key_env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "key_url": "https://platform.openai.com/api-keys",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "o3-mini", "o4-mini"],
        "default_model": "gpt-4o",
        "blurb": "OpenAI platform API key",
    },
    {
        "id": "codex_cli",
        "name": "Codex CLI",
        "family": "openai",
        "kind": "cli",
        "connect": "cli_oauth",
        "command": "codex",
        "models": ["codex (CLI default)"],
        "default_model": "codex (CLI default)",
        "blurb": "OpenAI Codex CLI if installed",
    },
    {
        "id": "mock",
        "name": "Mock (offline demo)",
        "family": "mock",
        "kind": "mock",
        "connect": "none",
        "models": ["mock"],
        "default_model": "mock",
        "blurb": "No key — for testing AETHER only",
    },
]


# Curated Open Code models when live list is unavailable
OPENCODE_FALLBACK_MODELS: list[str] = [
    "opencode/claude-sonnet-4-5",
    "opencode/claude-opus-4-6",
    "opencode/claude-haiku-4-5",
    "opencode/grok-4.5",
    "opencode/kimi-k2.7-code",
    "opencode/kimi-k2.6",
    "opencode/kimi-k3",
    "opencode/gemini-3.5-flash",
    "opencode/gemini-3.1-pro",
    "opencode/gpt-5.4",
    "opencode/gpt-5.2-codex",
    "opencode/deepseek-v4-pro",
    "opencode/qwen3.6-plus",
]


def _catalog_entry_from_market(p: "MarketProvider") -> dict[str, Any]:
    """Render a `config.providers` entry in the login catalogue's shape."""
    entry: dict[str, Any] = {
        "id": p.id,
        "name": p.name,
        "family": p.id,
        "kind": "cli" if p.kind == "cli" else "api",
        "connect": "cli_oauth" if p.kind == "cli" else ("none" if p.kind == "local" else "browser_key"),
        "models": [p.default_model] if p.default_model else [],
        "default_model": p.default_model,
        "blurb": p.blurb,
        "extended": True,
        "verified": p.verified,
    }
    if p.kind == "cli":
        entry["command"] = p.command
    else:
        entry["base_url"] = p.base_url
        if p.api_key_env:
            entry["api_key_env"] = p.api_key_env
    if p.key_url:
        entry["key_url"] = p.key_url
    return entry


def _extend_catalog() -> None:
    """Fold the wider market catalogue into the login catalogue.

    The curated lineup above stays hand-written — it carries real model lists
    and wording the wizard depends on. Everything else in `config.providers`
    is appended so `aether connect <id>` and `aether models` reach it, marked
    `extended` so the wizard can keep its short list short.
    """
    from aether.config.providers import ALL_PROVIDERS

    known = {c["id"] for c in PROVIDER_CATALOG}
    for p in ALL_PROVIDERS:
        if p.id in known or p.id == "generic_cli":
            continue
        PROVIDER_CATALOG.append(_catalog_entry_from_market(p))
        known.add(p.id)


_extend_catalog()


def catalog_by_id(provider_id: str) -> Optional[dict[str, Any]]:
    for c in PROVIDER_CATALOG:
        if c["id"] == provider_id:
            return c
    return None


def primary_login_providers() -> list[dict[str, Any]]:
    """Providers shown in the login wizard.

    Deliberately short. The full market catalogue is dozens of entries long;
    presenting all of it at first run is a worse experience than presenting
    five and pointing at `aether models` for the rest.
    """
    order = ["claude_cli", "gemini", "grok", "kimi", "opencode_cli"]
    by_id = {c["id"]: c for c in PROVIDER_CATALOG}
    out = [by_id[i] for i in order if i in by_id]
    for c in PROVIDER_CATALOG:
        if c["id"] not in order and not c.get("extended"):
            out.append(c)
    return out


def all_catalog_providers() -> list[dict[str, Any]]:
    """Every provider AETHER knows how to connect, curated and extended."""
    return list(PROVIDER_CATALOG)
