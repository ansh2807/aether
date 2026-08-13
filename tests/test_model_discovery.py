"""Asking providers which models they actually serve.

The catalogue's `default_model` strings age badly — `gpt-4o`, `grok-2-latest`,
`gemini-2.0-flash` were right when written. They fail in the worst direction: a
404 from a config file that looks authoritative.

Nothing here hardcodes replacements, because a "latest models" list baked into
this repo would go stale the same silent way. The provider is asked instead.
These tests therefore never assert a model *name* — only that whatever the
provider says is read correctly, and that not being able to ask is reported as
exactly that rather than as an empty list.
"""

from __future__ import annotations

import httpx
import pytest

from aether.config.discovery import (
    Discovery,
    _models_url,
    _parse,
    discover,
    discover_all,
    rank,
)
from aether.domain.models import ProviderConfig


def _cfg(base_url: str = "https://api.example.com/v1", key_env: str = "") -> ProviderConfig:
    return ProviderConfig(
        provider_id="p", display_name="P", base_url=base_url, api_key_env=key_env
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- URL building --------------------------------------------------------


@pytest.mark.parametrize("base,expected", [
    ("https://api.openai.com/v1", "https://api.openai.com/v1/models"),
    ("https://api.deepseek.com", "https://api.deepseek.com/v1/models"),
    ("https://x.com/v1/", "https://x.com/v1/models"),
    # Gemini's OpenAI shim, which does not end in /v1
    ("https://generativelanguage.googleapis.com/v1beta/openai",
     "https://generativelanguage.googleapis.com/v1beta/openai/models"),
])
def test_the_models_url_is_built_per_provider_shape(base: str, expected: str):
    """Appending /v1/models blindly produces /v1/v1/models against half the
    catalogue, which 404s and looks like the provider is down."""
    assert _models_url(base) == expected


def test_no_base_url_means_not_an_http_provider():
    assert _models_url("") == ""


# --- parsing whatever they send back -------------------------------------


def test_the_openai_shape_is_read():
    assert _parse({"data": [{"id": "a"}, {"id": "b"}]}) == ["a", "b"]


def test_the_ollama_shape_is_read():
    """Local runtimes answer `{"models": [{"name": ...}]}`. Insisting on the
    OpenAI shape would drop every locally served model on a technicality."""
    assert _parse({"models": [{"name": "llama3.2"}]}) == ["llama3.2"]


def test_junk_does_not_raise():
    for payload in (None, [], {}, {"data": "nope"}, {"data": [{}, 7]}, "text"):
        assert _parse(payload) == []


# --- what happens when you cannot ask ------------------------------------


@pytest.mark.asyncio
async def test_a_missing_key_is_reported_not_treated_as_empty():
    """The distinction that matters: "I could not ask" and "it serves nothing"
    lead to completely different next actions."""
    res = await discover(_cfg(key_env="NOPE_MISSING_KEY"))
    assert not res.ok
    assert "NOPE_MISSING_KEY" in res.error


@pytest.mark.asyncio
async def test_a_rejected_key_says_so():
    res = await discover(
        _cfg(), client=_client(lambda r: httpx.Response(401, json={}))
    )
    assert "401" in res.error


@pytest.mark.asyncio
async def test_a_provider_without_the_endpoint_says_so():
    res = await discover(_cfg(), client=_client(lambda r: httpx.Response(404)))
    assert "404" in res.error


@pytest.mark.asyncio
async def test_a_timeout_is_reported_not_raised():
    def handler(request):
        raise httpx.TimeoutException("too slow", request=request)

    res = await discover(_cfg(), client=_client(handler))
    assert not res.ok
    assert "timed out" in res.error


@pytest.mark.asyncio
async def test_a_connection_error_is_reported_not_raised():
    """A local runtime that is not running is the common case, and it must not
    take down a refresh across thirty providers."""
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    res = await discover(_cfg(), client=_client(handler))
    assert not res.ok


@pytest.mark.asyncio
async def test_malformed_json_is_reported_not_raised():
    res = await discover(
        _cfg(), client=_client(lambda r: httpx.Response(200, text="<html>"))
    )
    assert not res.ok


@pytest.mark.asyncio
async def test_reachable_and_genuinely_empty_is_distinct_from_unreachable():
    res = await discover(
        _cfg(), client=_client(lambda r: httpx.Response(200, json={"data": []}))
    )
    assert res.ok, "an empty list is an answer, not a failure"
    assert "no models" in res.summary


# --- the happy path ------------------------------------------------------


@pytest.mark.asyncio
async def test_models_come_back_from_a_live_endpoint():
    seen: dict = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"id": "some-new-model"}]})

    import os

    os.environ["DISCOVERY_TEST_KEY"] = "sk-test"
    try:
        res = await discover(_cfg(key_env="DISCOVERY_TEST_KEY"), client=_client(handler))
    finally:
        del os.environ["DISCOVERY_TEST_KEY"]

    assert res.ok and res.models == ["some-new-model"]
    assert seen["url"].endswith("/v1/models")
    assert seen["auth"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_a_model_this_repo_has_never_heard_of_is_returned_verbatim():
    """The whole point. Discovery must not filter against any list we hold,
    because the model released last week is the one being looked for."""
    unknown = "frontier-model-9.7-turbo"
    res = await discover(
        _cfg(), client=_client(lambda r: httpx.Response(200, json={"data": [{"id": unknown}]}))
    )
    assert res.models == [unknown]


@pytest.mark.asyncio
async def test_one_dead_provider_does_not_sink_the_others():
    def handler(request):
        if "dead" in str(request.url):
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"data": [{"id": "m"}]})

    results = await discover_all(
        [_cfg("https://alive.test/v1"), _cfg("https://dead.test/v1")],
        client=_client(handler),
    )
    assert len(results) == 2
    assert sum(r.ok for r in results) == 1


@pytest.mark.asyncio
async def test_discovering_nothing_is_fine():
    assert await discover_all([]) == []


# --- ordering ------------------------------------------------------------


def test_non_chat_models_sink_below_the_fold():
    """An OpenAI listing is mostly embeddings and audio; unsorted, they fill
    the first screen and hide what you came for."""
    out = rank(["text-embedding-3-large", "chat-model", "whisper-1", "tts-1"])
    assert out[0] == "chat-model"


def test_your_own_family_floats_up():
    out = rank(["zebra-1", "gpt-5", "aardvark-1"], hint="gpt-4o")
    assert out[0] == "gpt-5"


def test_ranking_never_drops_anything():
    """Sorting is a display concern. Losing a model here would be
    indistinguishable, to the user, from the provider not offering it."""
    models = ["a-embed", "b", "c-whisper", "d"]
    assert sorted(rank(models)) == sorted(models)


def test_ranking_handles_an_empty_list():
    assert rank([]) == []


# --- the reported summary ------------------------------------------------


def test_summary_reports_a_count_when_it_worked():
    assert "3 models" in Discovery("p", models=["a", "b", "c"]).summary


def test_summary_reports_the_error_when_it_did_not():
    assert Discovery("p", error="timed out").summary == "timed out"


# --- CLI providers -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_missing_binary_is_reported_not_raised():
    from aether.config.discovery import discover_cli

    res = await discover_cli("definitely-not-installed-xyz")
    assert not res.ok
    assert "not installed" in res.error


@pytest.mark.asyncio
async def test_a_cli_that_documents_model_is_read_verbatim(tmp_path, monkeypatch):
    """The text comes from whoever ships the CLI, so it stays true when they
    change it. Nothing here infers a model name — inferring one is what put
    "opus-5" in the help text and got it rejected by the real CLI."""
    import shutil as _shutil
    import sys

    from aether.config import discovery

    fake = tmp_path / "fakecli.py"
    fake.write_text(
        "print('''Usage: fake [opts]\n"
        "  --model <model>   Provide an alias (e.g. 'opus') or a full name.\n"
        "  --other           Something else.''')",
        encoding="utf-8",
    )
    launcher = tmp_path / ("fakecli.bat" if sys.platform == "win32" else "fakecli")
    if sys.platform == "win32":
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{fake}" %*\r\n', encoding="utf-8")
    else:
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)

    monkeypatch.setattr(
        discovery.shutil, "which",
        lambda c: str(launcher) if c == "fakecli" else _shutil.which(c),
    )
    res = await discovery.discover_cli("fakecli")
    assert res.ok, res.error
    assert "--model" in res.models[0]
    assert "opus" in res.models[0]
    assert "--other" not in res.models[0], "it should stop at the next option"


@pytest.mark.asyncio
async def test_a_cli_without_a_model_option_says_so(tmp_path, monkeypatch):
    import shutil as _shutil
    import sys

    from aether.config import discovery

    fake = tmp_path / "nomodel.py"
    fake.write_text("print('Usage: tool\n  --verbose  noisy')", encoding="utf-8")
    launcher = tmp_path / ("nomodel.bat" if sys.platform == "win32" else "nomodel")
    if sys.platform == "win32":
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{fake}" %*\r\n', encoding="utf-8")
    else:
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)

    monkeypatch.setattr(
        discovery.shutil, "which",
        lambda c: str(launcher) if c == "nomodel" else _shutil.which(c),
    )
    res = await discovery.discover_cli("nomodel")
    assert not res.ok
    assert "no --model" in res.error


@pytest.mark.asyncio
async def test_the_fallback_model_line_is_not_mistaken_for_it(tmp_path, monkeypatch):
    """`claude --help` lists --fallback-model before --model; matching the
    first line containing "--model" would return the wrong description."""
    import shutil as _shutil
    import sys

    from aether.config import discovery

    fake = tmp_path / "fb.py"
    fake.write_text(
        "print('''Usage: t\n"
        "  --fallback-model <m>   Used when the primary is overloaded.\n"
        "  --model <m>            The one you actually want.''')",
        encoding="utf-8",
    )
    launcher = tmp_path / ("fb.bat" if sys.platform == "win32" else "fb")
    if sys.platform == "win32":
        launcher.write_text(f'@echo off\r\n"{sys.executable}" "{fake}" %*\r\n', encoding="utf-8")
    else:
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)

    monkeypatch.setattr(
        discovery.shutil, "which",
        lambda c: str(launcher) if c == "fb" else _shutil.which(c),
    )
    res = await discovery.discover_cli("fb")
    assert res.ok
    assert "actually want" in res.models[0]
    assert "overloaded" not in res.models[0]
