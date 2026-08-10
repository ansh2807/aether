"""Context / text-limit tracking for chat sessions.

Estimates tokens, detects approaching provider context windows,
and decides when to compact (summarize) and rollover to a new session segment.
"""

from __future__ import annotations

from typing import Optional

# Conservative defaults when provider does not declare a window
DEFAULT_CONTEXT_TOKENS = 128_000
DEFAULT_COMPACTION_RATIO = 0.78  # start summary before hard limit
DEFAULT_KEEP_RECENT_MESSAGES = 6


def estimate_tokens(text: str) -> int:
    """Rough token estimate (chars/4). Good enough for routing/compaction."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def estimate_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        total += estimate_tokens(str(m.get("role", "")))
        total += estimate_tokens(str(m.get("content", "")))
        total += 4  # message framing overhead
    return total


class ContextBudget:
    def __init__(
        self,
        context_window: int = DEFAULT_CONTEXT_TOKENS,
        compaction_ratio: float = DEFAULT_COMPACTION_RATIO,
        keep_recent: int = DEFAULT_KEEP_RECENT_MESSAGES,
    ) -> None:
        self.context_window = max(1024, context_window)
        self.compaction_ratio = min(0.95, max(0.5, compaction_ratio))
        self.keep_recent = max(2, keep_recent)

    @property
    def soft_limit(self) -> int:
        return int(self.context_window * self.compaction_ratio)

    def usage(self, messages: list[dict], system: str = "", summary: str = "") -> dict:
        used = estimate_messages_tokens(messages)
        used += estimate_tokens(system)
        used += estimate_tokens(summary)
        return {
            "used_tokens": used,
            "context_window": self.context_window,
            "soft_limit": self.soft_limit,
            "ratio": used / self.context_window if self.context_window else 0.0,
            "needs_compaction": used >= self.soft_limit,
            "remaining": max(0, self.context_window - used),
        }

    def needs_compaction(self, messages: list[dict], system: str = "", summary: str = "") -> bool:
        return bool(self.usage(messages, system=system, summary=summary)["needs_compaction"])


# Per-provider default context windows (configurable; not absolute truth)
PROVIDER_CONTEXT_DEFAULTS: dict[str, int] = {
    "mock": 32_000,
    "openai": 128_000,
    "gemini": 1_000_000,
    "grok": 128_000,
    "kimi": 128_000,
    "claude_cli": 200_000,
    "codex_cli": 128_000,
    "opencode_cli": 128_000,
    "openai_compatible": 32_000,
}


def context_window_for(
    provider_id: str,
    override: Optional[int] = None,
    config_window: Optional[int] = None,
) -> int:
    """Resolve context window: explicit override > provider config > built-in defaults."""
    if override:
        return max(1024, int(override))
    if config_window:
        return max(1024, int(config_window))
    if provider_id in PROVIDER_CONTEXT_DEFAULTS:
        return PROVIDER_CONTEXT_DEFAULTS[provider_id]
    if provider_id.startswith("oc_"):
        return PROVIDER_CONTEXT_DEFAULTS["opencode_cli"]
    return DEFAULT_CONTEXT_TOKENS
