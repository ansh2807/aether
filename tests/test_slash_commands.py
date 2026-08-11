"""The `/` command palette: completion, help centre, and did-you-mean."""

from __future__ import annotations

import pytest

from aether.cli.commands import BY_NAME, CATEGORY_ORDER, COMMANDS, match, suggest
from aether.cli.prompt import TerminalPrompt, command_completions


# --- registry integrity ---------------------------------------------------


def test_every_command_is_well_formed():
    for cmd in COMMANDS:
        assert cmd.name.startswith("/"), cmd
        assert cmd.summary and cmd.summary[0].isupper(), cmd
        assert cmd.category in CATEGORY_ORDER, cmd


def test_no_duplicate_names_or_aliases():
    seen: set[str] = set()
    for cmd in COMMANDS:
        for token in (cmd.name, *cmd.aliases):
            assert token not in seen, f"{token} registered twice"
            seen.add(token)


def test_registry_covers_what_the_repl_dispatches():
    """Commands the terminal actually handles must be discoverable."""
    import pathlib
    import re

    source = pathlib.Path("src/aether/cli/terminal.py").read_text(encoding="utf-8")
    dispatched = set(re.findall(r'raw(?:\.startswith)?\s*(?:==|in)?\s*[({]?"(/[a-z]+)', source))
    missing = {c for c in dispatched if c not in BY_NAME}
    assert not missing, f"dispatched but not in the registry (so undiscoverable): {missing}"


# --- completion -----------------------------------------------------------


def test_slash_offers_everything():
    assert command_completions("/") == COMMANDS


@pytest.mark.parametrize(
    "typed,expected",
    [
        ("/ta", "/task"),
        ("/deb", "/debate"),
        ("/con", "/connect"),
        ("/doc", "/doctor"),
    ],
)
def test_prefix_narrows_to_the_right_command(typed: str, expected: str):
    names = [c.name for c in command_completions(typed)]
    assert expected in names


def test_alias_completes_to_its_command():
    # "/reconfigure" is an alias of "/setup"; typing the alias must find it
    assert "/setup" in [c.name for c in command_completions("/recon")]
    # "/catalog" is an alias of "/models"
    assert "/models" in [c.name for c in command_completions("/cata")]


def test_models_is_its_own_command_not_a_setup_alias():
    """`/models` used to re-run the wizard, which is not what the name promises."""
    names = [c.name for c in command_completions("/models")]
    assert names == ["/models"], f"expected the catalogue command, got {names}"


def test_no_menu_for_plain_chat():
    assert command_completions("how do I fix this error") == []
    assert command_completions("") == []


def test_no_menu_once_typing_arguments():
    """A menu over a half-written goal would be noise."""
    assert command_completions("/task Build a CLI") == []
    assert command_completions("/debate ") == []


def test_unknown_command_gets_a_suggestion():
    assert "/task" in [c.name for c in suggest("/tsk")]
    assert "/doctor" in [c.name for c in suggest("/doctr")]


def test_suggest_falls_back_to_prefix_matches():
    assert [c.name for c in suggest("/s")][:1] == ["/status"]


def test_match_is_case_insensitive():
    assert "/task" in [c.name for c in match("/TA")]


# --- prompt fallback ------------------------------------------------------


def test_prompt_falls_back_without_a_terminal():
    """Piped stdin must still work — the REPL cannot depend on a TTY."""
    prompt = TerminalPrompt()
    assert isinstance(prompt.menu_available, bool)


def test_help_centre_lists_every_command():
    from rich.console import Console

    from aether.cli.terminal import render_help
    from aether.cli.ui import AETHER_THEME

    buffer = Console(record=True, width=120, theme=AETHER_THEME)
    buffer.print(render_help())
    text = buffer.export_text()
    for cmd in COMMANDS:
        assert cmd.name in text, f"{cmd.name} missing from /help"


def test_getting_started_warns_about_the_mock():
    from rich.console import Console

    from aether.cli.terminal import render_getting_started
    from aether.cli.ui import AETHER_THEME

    buffer = Console(record=True, width=100, theme=AETHER_THEME)
    buffer.print(render_getting_started("mock", has_real_provider=False))
    text = buffer.export_text()
    assert "mock" in text
    assert "/setup" in text

    buffer = Console(record=True, width=100, theme=AETHER_THEME)
    buffer.print(render_getting_started("claude_cli", has_real_provider=True))
    assert "/setup" not in buffer.export_text()
