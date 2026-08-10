"""Input line for the interactive terminal, with a `/` command menu.

Typing `/` pops a filtered list of commands with descriptions, the way Claude
Code does it. That needs real terminal control, so it uses prompt_toolkit when
one is available and falls back to a plain prompt otherwise (piped stdin, dumb
terminals, or when the dependency is missing) — the REPL must still work.
"""

from __future__ import annotations

from typing import Any, Optional

from aether.cli.commands import COMMANDS, SlashCommand

try:  # pragma: no cover - exercised by the fallback path in tests
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import HTML

    _HAS_PTK = True
except Exception:  # noqa: BLE001
    PromptSession = None  # type: ignore[assignment]
    Completer = object  # type: ignore[assignment]
    _HAS_PTK = False


def command_completions(text: str) -> list[SlashCommand]:
    """Commands to offer for the current input. Empty unless typing a command.

    Only the first token completes: once there's a space the user is writing
    arguments (a topic, a goal) and a menu would be in the way.
    """
    if not text.startswith("/"):
        return []
    # a space (even a trailing one) means the command token is finished
    if " " in text or "\n" in text:
        return []
    token = text.strip()
    return [c for c in COMMANDS if c.matches(token)]


class SlashCompleter(Completer):  # type: ignore[misc]
    """Feeds `command_completions` into prompt_toolkit's menu."""

    def get_completions(self, document: Any, complete_event: Any):  # noqa: ANN001
        text = document.text_before_cursor
        matches = command_completions(text)
        if not matches:
            return
        token = text.strip()
        for cmd in matches:
            yield Completion(
                cmd.name,
                start_position=-len(token),
                display=cmd.display,
                display_meta=cmd.summary,
            )


class TerminalPrompt:
    """Reads one line, offering the command menu when possible."""

    def __init__(self) -> None:
        self.session: Optional[Any] = None
        self.rich_fallback = not _HAS_PTK
        if _HAS_PTK:
            try:
                self.session = PromptSession(
                    completer=SlashCompleter(),
                    complete_while_typing=True,
                    reserve_space_for_menu=8,
                )
            except Exception:  # noqa: BLE001 - no usable terminal
                self.session = None
                self.rich_fallback = True

    @property
    def menu_available(self) -> bool:
        return self.session is not None

    def ask(self, active_provider: str) -> str:
        """Return the next line. Raises EOFError/KeyboardInterrupt like input()."""
        if self.session is not None:
            try:
                return self.session.prompt(
                    HTML(
                        "<ansicyan><b>&#10095;</b></ansicyan> "
                        f"<ansibrightblack>({active_provider})</ansibrightblack> "
                    )
                )
            except (EOFError, KeyboardInterrupt):
                raise
            except Exception:  # noqa: BLE001 - terminal went away mid-session
                self.session = None
                self.rich_fallback = True

        from aether.cli.ui import PROMPT, console

        return console.input(f"[bold cyan]{PROMPT}[/] [dim]({active_provider})[/] ")
