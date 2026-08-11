"""The slash-command registry.

One source of truth for the interactive terminal: it drives the `/` completion
menu, `/help`, and the "unknown command" suggestion. Adding a command here makes
it discoverable everywhere at once.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SlashCommand:
    name: str
    summary: str
    category: str
    args: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)
    example: str = ""

    @property
    def display(self) -> str:
        return f"{self.name} {self.args}".strip()

    def matches(self, prefix: str) -> bool:
        prefix = prefix.lower()
        return self.name.startswith(prefix) or any(a.startswith(prefix) for a in self.aliases)


CHAT = "Chat"
MODELS = "Models & connection"
SESSIONS = "Sessions & memory"
ORCHESTRATOR = "Orchestrator"
SYSTEM = "System"

CATEGORY_ORDER = [CHAT, MODELS, SESSIONS, ORCHESTRATOR, SYSTEM]

COMMANDS: list[SlashCommand] = [
    # --- chat ---
    SlashCommand(
        "/help", "Show every command, grouped", CHAT,
        aliases=("/?", "help", "?"),
    ),
    SlashCommand(
        "/debate", "Several models argue a topic in rounds, then a consensus",
        CHAT, args="<topic>",
        example="/debate Postgres or SQLite for a desktop app",
    ),
    SlashCommand(
        "/mode", "Switch what plain typing does", CHAT, args="chat|debate|task",
        example="/mode task",
    ),
    # --- models ---
    SlashCommand(
        "/status", "Active model, provider pool, context usage", MODELS,
    ),
    SlashCommand(
        "/agents", "List enabled providers and their health", MODELS,
    ),
    SlashCommand(
        "/model", "Switch the active model right now", MODELS, args="<id>",
        example="/model claude_cli",
    ),
    SlashCommand(
        "/setup", "Re-run the wizard: pick platforms and models", MODELS,
        aliases=("/reconfigure",),
    ),
    SlashCommand(
        "/models", "Every provider AETHER can connect to, and which are ready here",
        MODELS, args="[cli|api|local|aggregator]",
        example="/models cli",
        aliases=("/catalog",),
    ),
    SlashCommand(
        "/connect", "Sign in to a provider (browser OAuth or API key)",
        MODELS, args="<id>",
        example="/connect claude_cli",
    ),
    # --- sessions ---
    SlashCommand(
        "/new", "Start a fresh chat session (keeps your providers)", SESSIONS,
    ),
    SlashCommand(
        "/sessions", "List saved chat sessions", SESSIONS,
    ),
    SlashCommand(
        "/resume", "Reopen a saved session by id", SESSIONS, args="<id>",
    ),
    SlashCommand(
        "/compact", "Summarise now and roll over to a new segment", SESSIONS,
    ),
    # --- orchestrator ---
    SlashCommand(
        "/task", "Full orchestration: plan, assign agents, verify",
        ORCHESTRATOR, args="<goal>",
        example="/task Build a CLI that renames photos by EXIF date",
    ),
    SlashCommand(
        "/collab", "Task where agents take specialist roles", ORCHESTRATOR, args="<goal>",
    ),
    SlashCommand(
        "/parallel", "Task split into independent workstreams", ORCHESTRATOR, args="<goal>",
    ),
    SlashCommand(
        "/tasks", "List orchestrator tasks and their status", ORCHESTRATOR,
    ),
    SlashCommand(
        "/inspect", "Timeline, handoffs and memory for one task",
        ORCHESTRATOR, args="<TASK-id>",
    ),
    # --- system ---
    SlashCommand(
        "/doctor", "Diagnose install, providers, keys and permissions", SYSTEM,
    ),
    SlashCommand(
        "/clear", "Clear the screen", SYSTEM, aliases=("clear",),
    ),
    SlashCommand(
        "/quit", "Save the session and exit", SYSTEM,
        aliases=("/exit", ":q", "quit", "exit"),
    ),
]

BY_NAME: dict[str, SlashCommand] = {}
for _cmd in COMMANDS:
    BY_NAME[_cmd.name] = _cmd
    for _alias in _cmd.aliases:
        BY_NAME.setdefault(_alias, _cmd)


def match(prefix: str) -> list[SlashCommand]:
    """Commands whose name or alias starts with `prefix`, best-first."""
    if not prefix or prefix == "/":
        return list(COMMANDS)
    return [c for c in COMMANDS if c.matches(prefix)]


def suggest(unknown: str) -> list[SlashCommand]:
    """Closest commands to something the user mistyped."""
    import difflib

    token = unknown.split()[0] if unknown.split() else unknown
    prefix_hits = match(token)
    if prefix_hits:
        return prefix_hits[:3]
    names = list(BY_NAME)
    close = difflib.get_close_matches(token, names, n=3, cutoff=0.5)
    seen: list[SlashCommand] = []
    for name in close:
        cmd = BY_NAME[name]
        if cmd not in seen:
            seen.append(cmd)
    return seen
