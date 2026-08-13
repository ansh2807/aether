"""Choosing a model on a subscription CLI.

`ProviderConfig.model` existed and the CLI adapter ignored it, so the eight
CLI-authenticated providers could only ever run whatever their binary defaults
to. The one place you could not pick the newest model was the login that
entitles you to it.

No allowlist of model names here, deliberately. Vendors ship faster than a
hardcoded catalogue stays true, and a stale list that rejects a model you are
paying for is worse than no list at all. The string is passed through and the
CLI validates it — which is also why these tests name models this codebase has
never heard of.
"""

from __future__ import annotations

import pytest

from aether.adapters.cli_adapter import CliAgentAdapter
from aether.domain.enums import AdapterType
from aether.domain.models import ProviderConfig


def _adapter(**kw) -> CliAgentAdapter:
    return CliAgentAdapter(
        ProviderConfig(
            provider_id="claude_cli",
            display_name="Claude CLI",
            adapter=AdapterType.CLI,
            command="claude",
            **kw,
        )
    )


def test_a_configured_model_reaches_the_command_line():
    assert _adapter(model="opus-5").build_argv("claude") == ["claude", "--model", "opus-5"]


def test_the_cli_default_sentinel_adds_no_flag():
    """The catalogue stores "claude (CLI default)" to mean *whatever you are
    logged into*. Passing that literal as a model name would break every CLI
    provider out of the box, which is the regression this pins."""
    assert _adapter(model="claude (CLI default)").build_argv("claude") == ["claude"]


def test_no_model_configured_adds_no_flag():
    assert _adapter().build_argv("claude") == ["claude"]


def test_a_blank_model_adds_no_flag():
    assert _adapter(model="   ").build_argv("claude") == ["claude"]


def test_existing_args_are_preserved():
    argv = _adapter(model="opus-5", args=["--verbose"]).build_argv("claude")
    assert argv == ["claude", "--verbose", "--model", "opus-5"]


def test_a_hand_written_flag_wins():
    """Someone who put --model in args meant it. Adding a second one makes
    most CLIs reject the whole invocation."""
    argv = _adapter(model="opus-5", args=["--model", "sonnet-5"]).build_argv("claude")
    assert argv.count("--model") == 1
    assert "sonnet-5" in argv and "opus-5" not in argv


def test_a_provider_that_spells_the_flag_differently():
    argv = _adapter(model="grok-4.5", extra={"model_flag": "-m"}).build_argv("grok")
    assert argv == ["grok", "-m", "grok-4.5"]


def test_a_provider_that_takes_no_model_flag():
    """An empty flag disables passthrough for a CLI that has no such option,
    rather than forcing an argument it will reject."""
    assert _adapter(model="x", extra={"model_flag": ""}).build_argv("c") == ["c"]


@pytest.mark.parametrize("name", [
    "opus-5", "gpt-5.6", "grok-4.5", "gemini-3-pro", "a-model-shipped-tomorrow",
])
def test_any_model_name_is_accepted(name: str):
    """The case that actually matters is the model released last week, which
    no allowlist in this repo could know about."""
    assert _adapter(model=name).build_argv("claude")[-1] == name


def test_the_resolved_path_is_used_not_the_bare_command():
    """`execute` resolves the binary before building argv; argv[0] has to be
    that resolved path or the spawn misses it on Windows."""
    assert _adapter(model="opus-5").build_argv("/usr/local/bin/claude")[0] == (
        "/usr/local/bin/claude"
    )
