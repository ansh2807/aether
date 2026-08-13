"""The provider catalogue: what AETHER can talk to, and how sure we are.

Almost every hosted model today speaks the OpenAI chat-completions shape, and
almost every coding agent ships a CLI. AETHER already has one adapter for each
of those, so adding a provider is configuration, not code — which is what makes
a catalogue this size reasonable to maintain.

Two rules keep this list honest, both from the project's own principles:

1. **Nothing here is enabled by default.** A provider appears when you connect
   it, not because it is listed.
2. **`verified` says whether the endpoint has actually been exercised**, not
   whether it is expected to work. Most entries are `False`: the endpoint and
   auth shape are recorded from the provider's own documentation, but no key
   was available to prove it. `aether connect <id>` is what turns a guess into
   a fact, and `aether models` shows you which is which.

Getting an entry wrong costs a confusing error at connect time, not silent
wrong behaviour: an unreachable base_url fails health checks and the router
routes around it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

CODING = {"coding": 9, "reasoning": 8, "testing": 8, "review": 8, "architecture": 8, "terminal": 5}
FRONTIER = {"coding": 9, "reasoning": 9, "research": 8, "testing": 8, "review": 9, "architecture": 9, "terminal": 5}
FAST = {"coding": 7, "reasoning": 7, "research": 6, "testing": 6, "review": 6, "architecture": 6, "terminal": 4}
RESEARCH = {"research": 9, "reasoning": 8, "review": 7, "coding": 6, "architecture": 6, "testing": 5}
AGENT_CLI = {"coding": 9, "reasoning": 9, "testing": 9, "terminal": 10, "review": 8, "architecture": 8}


@dataclass(frozen=True)
class MarketProvider:
    id: str
    name: str
    kind: str                      # "api" | "cli" | "local" | "aggregator"
    blurb: str
    base_url: str = ""
    api_key_env: str = ""
    default_model: str = ""
    command: str = ""
    key_url: str = ""
    context_window: Optional[int] = None
    capabilities: dict[str, int] = field(default_factory=lambda: dict(FAST))
    roles_preferred: tuple[str, ...] = ()
    # Has this endpoint actually been exercised from AETHER? Almost always no.
    verified: bool = False
    note: str = ""
    # What it costs to use. "free" means genuinely nothing (local hardware);
    # "free_tier" means a usable allowance before payment; "subscription"
    # means an existing plan with no per-token charge; "paid" means metered.
    #
    # Free tiers move constantly — providers add, shrink and withdraw them —
    # so this is a documented starting point, never a promise. `verified`
    # stays the flag for "we actually called it".
    cost: str = "paid"
    # A model on this provider known to be usable at no cost, where the
    # default is not. OpenRouter is the case that matters: its default is a
    # paid frontier model, so asking for "free" and getting billed would be a
    # nasty surprise.
    free_model: str = ""


# --- hosted APIs, OpenAI chat-completions shape --------------------------

API_PROVIDERS: list[MarketProvider] = [
    MarketProvider(
        "openai", "OpenAI", "api",
        "GPT models direct from OpenAI",
        base_url="https://api.openai.com/v1", api_key_env="OPENAI_API_KEY",
        default_model="gpt-5.6", key_url="https://platform.openai.com/api-keys",
        context_window=128_000, capabilities={**FRONTIER, "image": 8},
        roles_preferred=("architect", "reviewer", "implementer"),
    ),
    MarketProvider(
        "anthropic", "Anthropic (Claude API)", "api",
        "Claude via API key. For a Claude subscription use claude_cli instead.",
        base_url="https://api.anthropic.com/v1", api_key_env="ANTHROPIC_API_KEY",
        default_model="claude-opus-5", key_url="https://console.anthropic.com/settings/keys",
        context_window=200_000, capabilities=dict(FRONTIER),
        roles_preferred=("architect", "implementer", "reviewer", "debugger"),
        note="Anthropic's native API is not the OpenAI shape; needs its OpenAI-compatible endpoint or a dedicated adapter.",
    ),
    MarketProvider(
        "gemini", "Google Gemini", "api",
        "Gemini via the OpenAI-compatible endpoint",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key_env="GEMINI_API_KEY", default_model="gemini-3.6-flash",
        key_url="https://aistudio.google.com/apikey",
        context_window=1_000_000, capabilities=dict(FRONTIER),
        roles_preferred=("researcher", "tester", "implementer"),
        cost="free_tier", free_model="gemini-2.0-flash",
    ),
    MarketProvider(
        "grok", "Grok (xAI)", "api",
        "Grok models from xAI",
        base_url="https://api.x.ai/v1", api_key_env="XAI_API_KEY",
        default_model="grok-4.6", key_url="https://console.x.ai/",
        context_window=128_000, capabilities=dict(FRONTIER),
        roles_preferred=("researcher", "architect"),
    ),
    MarketProvider(
        "deepseek", "DeepSeek", "api",
        "Strong coding models, low cost",
        base_url="https://api.deepseek.com", api_key_env="DEEPSEEK_API_KEY",
        default_model="deepseek-chat", key_url="https://platform.deepseek.com/api_keys",
        context_window=64_000, capabilities=dict(CODING),
        roles_preferred=("implementer", "debugger"),
    ),
    MarketProvider(
        "mistral", "Mistral", "api",
        "Mistral hosted models",
        base_url="https://api.mistral.ai/v1", api_key_env="MISTRAL_API_KEY",
        default_model="mistral-large-latest", key_url="https://console.mistral.ai/api-keys/",
        context_window=128_000, capabilities=dict(CODING),
        roles_preferred=("implementer",),
        cost="free_tier", free_model="mistral-small-latest",
    ),
    MarketProvider(
        "groq", "Groq", "api",
        "Open models at very high tokens/sec",
        base_url="https://api.groq.com/openai/v1", api_key_env="GROQ_API_KEY",
        default_model="llama-3.3-70b-versatile", key_url="https://console.groq.com/keys",
        context_window=128_000, capabilities=dict(FAST),
        roles_preferred=("tester", "researcher"),
        cost="free_tier", free_model="llama-3.3-70b-versatile",
    ),
    MarketProvider(
        "together", "Together AI", "api",
        "Many open-weight models behind one key",
        base_url="https://api.together.xyz/v1", api_key_env="TOGETHER_API_KEY",
        default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        key_url="https://api.together.ai/settings/api-keys",
        context_window=128_000, capabilities=dict(FAST),
        cost="free_tier", free_model="meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
    ),
    MarketProvider(
        "fireworks", "Fireworks AI", "api",
        "Fast open-weight inference",
        base_url="https://api.fireworks.ai/inference/v1", api_key_env="FIREWORKS_API_KEY",
        default_model="accounts/fireworks/models/llama-v3p3-70b-instruct",
        key_url="https://fireworks.ai/account/api-keys",
        context_window=128_000, capabilities=dict(FAST),
    ),
    MarketProvider(
        "perplexity", "Perplexity", "api",
        "Search-grounded answers with citations — suits research subtasks",
        base_url="https://api.perplexity.ai", api_key_env="PERPLEXITY_API_KEY",
        default_model="sonar", key_url="https://www.perplexity.ai/settings/api",
        context_window=128_000, capabilities=dict(RESEARCH),
        roles_preferred=("researcher",),
    ),
    MarketProvider(
        "kimi", "Kimi (Moonshot)", "api",
        "Moonshot's long-context models",
        base_url="https://api.moonshot.cn/v1", api_key_env="KIMI_API_KEY",
        default_model="moonshot-v1-auto", key_url="https://platform.moonshot.cn/console/api-keys",
        context_window=128_000, capabilities=dict(CODING),
        roles_preferred=("implementer", "researcher"),
    ),
    MarketProvider(
        "glm", "GLM (Z.AI / Zhipu)", "api",
        "GLM-4.5-Flash is free with no token cap — the strongest genuinely-free "
        "hosted model in this catalogue",
        base_url="https://api.z.ai/api/paas/v4", api_key_env="ZHIPU_API_KEY",
        default_model="glm-5.2", key_url="https://z.ai/manage-apikey/apikey-list",
        context_window=128_000, capabilities=dict(CODING),
        roles_preferred=("implementer", "tester"),
        cost="free_tier", free_model="glm-4.5-flash",
        note="Mainland accounts use https://open.bigmodel.cn/api/paas/v4 instead.",
    ),
    MarketProvider(
        "cerebras", "Cerebras", "api",
        "Free tier on very fast open-weight inference",
        base_url="https://api.cerebras.ai/v1", api_key_env="CEREBRAS_API_KEY",
        default_model="llama-3.3-70b", key_url="https://cloud.cerebras.ai/",
        context_window=128_000, capabilities=dict(FAST),
        roles_preferred=("tester",),
        cost="free_tier", free_model="llama-3.3-70b",
    ),
    MarketProvider(
        "qwen", "Qwen (DashScope)", "api",
        "Alibaba's Qwen models, OpenAI-compatible mode",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY", default_model="qwen-max",
        key_url="https://dashscope.console.aliyun.com/apiKey",
        context_window=128_000, capabilities=dict(CODING),
    ),
]

# --- aggregators: one key, many models -----------------------------------

AGGREGATORS: list[MarketProvider] = [
    MarketProvider(
        "openrouter", "OpenRouter", "aggregator",
        "One key, hundreds of models from every major lab — the shortest path "
        "to 'all the models' without an account per vendor",
        base_url="https://openrouter.ai/api/v1", api_key_env="OPENROUTER_API_KEY",
        default_model="anthropic/claude-opus-5", key_url="https://openrouter.ai/keys",
        context_window=200_000, capabilities=dict(FRONTIER),
        roles_preferred=("architect", "implementer", "reviewer"),
        cost="free_tier", free_model="deepseek/deepseek-r1:free",
    ),
    MarketProvider(
        "omniroute", "OmniRoute (local gateway)", "aggregator",
        "A self-hosted gateway fronting ~290 providers with its own fallback "
        "and prompt compression — run it locally and AETHER treats the whole "
        "lot as one provider",
        base_url="http://127.0.0.1:8080/v1", api_key_env="OMNIROUTE_API_KEY",
        default_model="auto", key_url="https://github.com/diegosouzapw/OmniRoute",
        context_window=128_000, capabilities={**FRONTIER, "image": 8},
        roles_preferred=("implementer", "researcher"),
        note="Breadth via a gateway you run: its provider terms are its own to honour, not ours to restate.",
        cost="free_tier",
    ),
    MarketProvider(
        "openai_compatible", "Any OpenAI-compatible endpoint", "api",
        "Point base_url at anything speaking /chat/completions",
        base_url="http://127.0.0.1:11434/v1", api_key_env="OPENAI_API_KEY",
        default_model="llama3", context_window=32_000, capabilities=dict(FAST),
    ),
]

# --- local runtimes: no key, no data leaving the machine ------------------

LOCAL_PROVIDERS: list[MarketProvider] = [
    MarketProvider(
        "ollama", "Ollama (local)", "local",
        "Local models, no API key, nothing leaves the machine",
        base_url="http://127.0.0.1:11434/v1", api_key_env="",
        default_model="llama3.2", key_url="https://ollama.com/download",
        context_window=32_000, capabilities=dict(FAST),
        cost="free",
    ),
    MarketProvider(
        "lmstudio", "LM Studio (local)", "local",
        "LM Studio's local server",
        base_url="http://127.0.0.1:1234/v1", api_key_env="",
        default_model="local-model", key_url="https://lmstudio.ai/",
        context_window=32_000, capabilities=dict(FAST),
        cost="free",
    ),
    MarketProvider(
        "vllm", "vLLM (self-hosted)", "local",
        "Your own vLLM server",
        base_url="http://127.0.0.1:8000/v1", api_key_env="",
        default_model="served-model", key_url="https://docs.vllm.ai/",
        context_window=32_000, capabilities=dict(FAST),
        cost="free",
    ),
]

# --- agent CLIs: already authenticated on your machine --------------------
#
# These are driven as subprocesses using the auth you already have. AETHER
# never scrapes their credentials or converts a subscription into an API.

CLI_PROVIDERS: list[MarketProvider] = [
    MarketProvider(
        "claude_cli", "Claude Code", "cli",
        "Anthropic's coding agent, using your existing login",
        command="claude", default_model="claude (CLI default)",
        key_url="https://claude.ai/login", context_window=200_000,
        capabilities=dict(AGENT_CLI), verified=True,
        roles_preferred=("architect", "implementer", "reviewer", "debugger"),
        cost="subscription",
    ),
    MarketProvider(
        "codex_cli", "Codex CLI", "cli",
        "OpenAI's coding agent CLI",
        command="codex", default_model="codex (CLI default)",
        key_url="https://platform.openai.com/", context_window=128_000,
        capabilities=dict(AGENT_CLI),
        roles_preferred=("implementer", "tester", "debugger"),
        cost="subscription",
    ),
    MarketProvider(
        "gemini_cli", "Gemini CLI", "cli",
        "Google's terminal agent",
        command="gemini", default_model="gemini (CLI default)",
        key_url="https://github.com/google-gemini/gemini-cli",
        context_window=1_000_000, capabilities=dict(AGENT_CLI),
        roles_preferred=("researcher", "implementer"),
        cost="subscription",
    ),
    MarketProvider(
        "opencode_cli", "Open Code", "cli",
        "Multi-model hub — pick models at login",
        command="opencode", default_model="opencode/claude-opus-5",
        key_url="https://opencode.ai/", context_window=128_000,
        capabilities=dict(AGENT_CLI),
        roles_preferred=("implementer", "architect", "tester", "debugger"),
        cost="subscription",
    ),
    MarketProvider(
        "grok_cli", "Grok CLI", "cli",
        "xAI's terminal agent",
        command="grok", default_model="grok (CLI default)",
        key_url="https://x.ai/", context_window=128_000,
        capabilities=dict(AGENT_CLI),
        cost="subscription",
    ),
    MarketProvider(
        "aider", "Aider", "cli",
        "Pair-programming agent that edits your git repo",
        command="aider", default_model="aider (CLI default)",
        key_url="https://aider.chat/", context_window=128_000,
        capabilities=dict(AGENT_CLI),
        roles_preferred=("implementer", "debugger"),
        cost="subscription",
    ),
    MarketProvider(
        "cursor_cli", "Cursor CLI", "cli",
        "Cursor's terminal agent",
        command="cursor-agent", default_model="cursor (CLI default)",
        key_url="https://cursor.com/", context_window=128_000,
        capabilities=dict(AGENT_CLI),
        cost="subscription",
    ),
    MarketProvider(
        "copilot_cli", "GitHub Copilot CLI", "cli",
        "Copilot in the terminal",
        command="copilot", default_model="copilot (CLI default)",
        key_url="https://github.com/features/copilot",
        context_window=128_000, capabilities=dict(AGENT_CLI),
        cost="subscription",
    ),
    MarketProvider(
        "generic_cli", "Any other CLI agent", "cli",
        "Set command/args and AETHER will drive it over stdin",
        command="", default_model="", capabilities=dict(AGENT_CLI),
    ),
]

ALL_PROVIDERS: list[MarketProvider] = [
    *CLI_PROVIDERS,
    *AGGREGATORS,
    *API_PROVIDERS,
    *LOCAL_PROVIDERS,
]

BY_ID: dict[str, MarketProvider] = {p.id: p for p in ALL_PROVIDERS}

KIND_ORDER = ("cli", "aggregator", "api", "local")
KIND_LABEL = {
    "cli": "Agent CLIs (use the login you already have)",
    "aggregator": "Aggregators (one key, many models)",
    "api": "Hosted APIs (bring a key)",
    "local": "Local runtimes (no key, nothing leaves the machine)",
}


def local_endpoint_is_up(base_url: str, timeout: float = 0.15) -> bool:
    """Is something listening on a local runtime's port?

    A local provider needs no key, so a key check says nothing about it. Without
    this the readiness column would show every local runtime as ready whether or
    not the server was running — a green tick that means nothing is worse than
    no tick. This only proves the port is open, which is why `connect` still
    does the real check.
    """
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    if not parsed.hostname:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


def is_ready(provider: MarketProvider, env: Optional[dict] = None) -> bool:
    """Can this machine reach the provider right now, as far as we can cheaply tell?"""
    import os
    import shutil

    environ = os.environ if env is None else env
    if provider.kind == "cli":
        return bool(provider.command and shutil.which(provider.command))
    if provider.kind == "local":
        return local_endpoint_is_up(provider.base_url)
    return bool(provider.api_key_env and environ.get(provider.api_key_env))


FREE_COSTS = frozenset({"free", "free_tier"})

COST_LABEL = {
    "free": "free",
    "free_tier": "free tier",
    "subscription": "your plan",
    "paid": "paid",
}


def is_free(provider: MarketProvider) -> bool:
    """Usable without per-token charges.

    `subscription` is excluded on purpose. A Claude or Copilot plan has no
    marginal cost per request, but calling it "free" to someone asking for free
    models would be answering a different question than the one asked.
    """
    return provider.cost in FREE_COSTS


def free_model_for(provider: MarketProvider) -> str:
    """The model to use if the point is to spend nothing."""
    return provider.free_model or provider.default_model


def selectable() -> list[MarketProvider]:
    """Everything worth listing — excludes the bring-your-own escape hatch."""
    return [p for p in ALL_PROVIDERS if p.id != "generic_cli"]


def get(provider_id: str) -> Optional[MarketProvider]:
    return BY_ID.get(provider_id)


def by_kind(kind: str) -> list[MarketProvider]:
    return [p for p in ALL_PROVIDERS if p.kind == kind]


def to_provider_config(provider: MarketProvider, enabled: bool = False) -> dict:
    """Config-file shape for this provider, disabled unless asked otherwise."""
    adapter = "cli" if provider.kind == "cli" else "api"
    entry: dict = {
        "display_name": provider.name,
        "adapter": adapter,
        "enabled": enabled,
        "capabilities": dict(provider.capabilities),
    }
    if provider.roles_preferred:
        entry["roles_preferred"] = list(provider.roles_preferred)
    if provider.context_window:
        entry["context_window"] = provider.context_window
    if adapter == "cli":
        entry["command"] = provider.command
        entry["args"] = []
    else:
        entry["base_url"] = provider.base_url
        entry["model"] = provider.default_model
        if provider.api_key_env:
            entry["api_key_env"] = provider.api_key_env
    if provider.note:
        entry["status_note"] = provider.note
    return entry
