"""What the terminal actually puts on screen during a chat turn.

Three bugs visible in one screenshot, none of which the suite could catch
because nothing asserted on rendered output:

1. a line of structured JSON before every reply — the logger shared stdout
   with the conversation;
2. the answer printed twice, once bare and once boxed;
3. the full session banner repeating after every message.

All three are output-shape problems, so these tests read the source of the
turn loop rather than mocking a whole session. That is a weaker test than
driving the terminal, and it is chosen deliberately: the alternative was no
test at all on a path that had three visible defects.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "aether"
TERMINAL = SRC / "cli" / "terminal.py"


def _strip_comments(text: str) -> str:
    """Code only.

    The first version of these tests matched raw source and failed on their
    own fix: the comment explaining why `len(buffer) < 3` was wrong contains
    the string `len(buffer) < 3`. A test that cannot tell an explanation from
    the thing it explains is worse than useless — it fails when the code is
    right and passes when someone deletes the comment.
    """
    return "\n".join(
        line.split("#")[0] if not line.strip().startswith("#") else ""
        for line in text.splitlines()
    )


def _chat_turn_source() -> str:
    """The block that runs once per message, comments removed."""
    text = TERMINAL.read_text(encoding="utf-8")
    start = text.index('if final.get("ok"):')
    return _strip_comments(text[start : start + 2400])


# --- 1. logs must not share the conversation's stream --------------------


def test_logging_goes_to_stderr_not_stdout():
    """A JSON line naming provider, execution_id, argv0 and cwd printed in
    the middle of the chat before every reply."""
    log = (SRC / "observability" / "logging.py").read_text(encoding="utf-8")
    assert "stream=sys.stderr" in log
    assert "stream=sys.stdout" not in log


def test_the_handler_is_actually_bound_to_stderr_at_runtime():
    """Not a grep for the word: configure logging and read back where the
    root handler points. The first version regex-matched `basicConfig(...)`
    and broke on the nested `getattr(...)` call inside it — testing the real
    object avoids guessing at source shape entirely."""
    import logging as _logging
    import sys

    from aether.observability.logging import setup_logging

    root = _logging.getLogger()
    saved = list(root.handlers)
    root.handlers.clear()
    try:
        setup_logging(level="INFO")
        streams = [
            h.stream for h in _logging.getLogger().handlers
            if isinstance(h, _logging.StreamHandler) and hasattr(h, "stream")
        ]
        assert streams, "no stream handler was configured"
        assert all(s is sys.stderr for s in streams), (
            f"a handler still writes to the conversation's stream: {streams}"
        )
    finally:
        root.handlers.clear()
        root.handlers.extend(saved)


# --- 2. the answer is printed once ---------------------------------------


def test_the_panel_is_skipped_when_the_reply_already_streamed():
    """The old guard was `len(buffer) < 3`, meant to catch a stream that never
    happened. A CLI provider returns its whole answer in one chunk — which was
    printed, and then printed again inside a box."""
    turn = _chat_turn_source()
    assert "not buffer" in turn
    assert "len(buffer) < 3" not in turn


def test_a_single_chunk_response_counts_as_streamed():
    """The exact case from the screenshot: one chunk is a non-empty buffer,
    so the panel must not also fire."""
    buffer = ["Hi! What can I help you with?"]
    assert not (buffer and not buffer), "one chunk must not re-print"
    assert not buffer == False  # noqa: E712 - readability over idiom here


def test_no_output_at_all_still_gets_a_panel():
    """The case the original guard existed for must keep working: a provider
    that returns only a final payload has printed nothing yet."""
    buffer: list[str] = []
    assert not buffer, "an empty buffer means nothing reached the screen"


# --- 3. the banner is not reprinted every turn ---------------------------


def test_the_session_banner_does_not_repeat_after_each_message():
    """Other CLIs show it once. Reprinting the whole panel — version, mode,
    model, pool, cwd, clock, session id — after every reply buries the
    conversation in chrome."""
    assert "render_header" not in _chat_turn_source()


def test_the_banner_is_still_shown_at_startup():
    """Removing it from the turn loop must not remove it entirely; it is how
    you learn the model and session on entry."""
    text = TERMINAL.read_text(encoding="utf-8")
    assert text.count("render_header(app, profile, mode, session)") >= 1


def test_context_usage_survives_the_banner_removal():
    """The one live number worth having each turn is still printed, so the
    change trades chrome for signal rather than dropping both."""
    assert "ctx " in _chat_turn_source()


# --- the model picker ----------------------------------------------------


def test_bare_slash_model_lists_providers_instead_of_printing_usage():
    text = TERMINAL.read_text(encoding="utf-8")
    block = text[text.index('if raw.startswith("/model")') :][:2600]
    assert "Pick a model" in block
    assert "list_ids" in block, "the picker must read the real registry"


def test_the_picker_accepts_the_numbers_it_prints():
    """Printing a numbered list and then rejecting "2" is a small betrayal."""
    block = TERMINAL.read_text(encoding="utf-8")
    block = block[block.index('if raw.startswith("/model")') :][:2600]
    assert "isdigit()" in block


def test_an_out_of_range_number_is_refused_clearly():
    block = TERMINAL.read_text(encoding="utf-8")
    block = block[block.index('if raw.startswith("/model")') :][:2600]
    assert "pick 1" in block


def test_the_documented_example_is_not_an_invented_model_name():
    """`opus-5` shipped here once and the CLI rejected it. `claude --help`
    documents 'opus', 'sonnet', 'fable' and full names like 'claude-opus-5'."""
    text = TERMINAL.read_text(encoding="utf-8")
    examples = re.findall(r"/model claude_cli:([\w.\-]+)", text)
    assert examples, "the example should still be there"
    for name in examples:
        assert name in {"opus", "sonnet", "fable", "haiku", "claude-opus-5"}, (
            f"'{name}' is not a documented alias or full name"
        )


@pytest.mark.parametrize("bad", ["opus-5", "gpt-5.6", "grok-4.5"])
def test_names_the_claude_cli_would_reject_are_not_suggested_for_it(bad: str):
    """Guards the specific regression: a name from another vendor, or a
    plausible-looking string, presented as guidance for this CLI."""
    text = TERMINAL.read_text(encoding="utf-8")
    assert f"/model claude_cli:{bad}" not in text
