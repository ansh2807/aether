"""The market catalogue must stay coherent as it grows.

A catalogue this size is mostly data, and data rots quietly: a duplicated id
shadows a provider, a missing base_url produces a baffling error at connect
time, an over-claimed `verified` flag tells the user a guess is a fact. These
tests do not check that the endpoints work — that needs keys we do not have —
they check the claims the catalogue makes about itself.
"""

from __future__ import annotations

import pytest

from aether.config.providers import (
    ALL_PROVIDERS,
    BY_ID,
    KIND_LABEL,
    KIND_ORDER,
    to_provider_config,
)
from aether.config.session import PROVIDER_CATALOG, catalog_by_id, primary_login_providers


def test_ids_are_unique():
    ids = [p.id for p in ALL_PROVIDERS]
    assert len(ids) == len(set(ids)), f"duplicate ids: {sorted({i for i in ids if ids.count(i) > 1})}"
    assert len(BY_ID) == len(ALL_PROVIDERS)


def test_every_kind_is_one_we_render():
    for p in ALL_PROVIDERS:
        assert p.kind in KIND_ORDER, f"{p.id} has kind {p.kind!r}, which no group renders"
        assert p.kind in KIND_LABEL


@pytest.mark.parametrize("provider", [p for p in ALL_PROVIDERS if p.kind in {"api", "aggregator"}],
                         ids=lambda p: p.id)
def test_api_providers_are_connectable(provider):
    """Anything the user can pick must carry enough to actually build a client."""
    assert provider.base_url.startswith("http"), f"{provider.id}: base_url is not a URL"
    assert provider.default_model, f"{provider.id}: no default model, connect would leave it blank"
    assert provider.api_key_env, f"{provider.id}: hosted API with no key variable"
    assert provider.api_key_env.isupper(), f"{provider.id}: {provider.api_key_env} is not an env var name"


@pytest.mark.parametrize("provider", [p for p in ALL_PROVIDERS if p.kind == "local"],
                         ids=lambda p: p.id)
def test_local_providers_are_local(provider):
    """A 'nothing leaves the machine' promise that points at a remote host is a lie."""
    assert provider.base_url.startswith(("http://127.0.0.1", "http://localhost")), (
        f"{provider.id} is listed as local but points at {provider.base_url}"
    )
    assert not provider.api_key_env, f"{provider.id} is local but asks for a key"


@pytest.mark.parametrize("provider", [p for p in ALL_PROVIDERS if p.kind == "cli"],
                         ids=lambda p: p.id)
def test_cli_providers_name_a_command(provider):
    if provider.id == "generic_cli":
        pytest.skip("the escape hatch: the user supplies the command")
    assert provider.command, f"{provider.id}: no command to run"
    assert " " not in provider.command, f"{provider.id}: command should be the binary, args go elsewhere"


def test_capabilities_are_in_range():
    for p in ALL_PROVIDERS:
        for role, score in p.capabilities.items():
            assert 0 <= score <= 10, f"{p.id}.{role} = {score} is outside the router's 0-10 scale"


def test_verified_is_not_claimed_casually():
    """`verified` means exercised. Most entries are documentation, and the UI
    tells the user so — if this count balloons, the disclaimer became false."""
    verified = [p.id for p in ALL_PROVIDERS if p.verified]
    assert verified == ["claude_cli"], (
        f"unexpected verified providers: {verified}. Mark verified only after "
        "actually calling the endpoint from AETHER."
    )


# --- integration with the login catalogue --------------------------------


def test_market_providers_reach_the_connect_flow():
    """`aether connect deepseek` has to find something."""
    for p in ALL_PROVIDERS:
        if p.id == "generic_cli":
            continue
        assert catalog_by_id(p.id) is not None, f"{p.id} is listed but cannot be connected"


def test_curated_entries_are_not_overwritten():
    """The hand-written lineup carries real model lists; extension must not flatten it."""
    claude = catalog_by_id("claude_cli")
    assert claude is not None and len(claude["models"]) > 1
    assert not claude.get("extended")

    gemini = catalog_by_id("gemini")
    assert gemini is not None and "gemini-2.5-pro" in gemini["models"]


def test_login_wizard_stays_short():
    """The whole point of the `extended` flag: first run must not be a wall of 25 options."""
    primary = primary_login_providers()
    assert len(primary) <= 10, (
        f"the login wizard now offers {len(primary)} providers; that is a worse "
        "first run than five plus `aether models`"
    )
    assert {p["id"] for p in primary} >= {"claude_cli", "gemini", "grok", "kimi", "opencode_cli"}
    assert "mock" in {p["id"] for p in primary}, "the offline demo path must stay discoverable"


def test_extension_is_idempotent():
    """session.py extends a module-level list in place; running it twice must
    not duplicate entries.

    Deliberately does not `importlib.reload` the module to test this: reloading
    rebinds `session.PROVIDER_CATALOG` to a fresh list while every module that
    did `from ... import PROVIDER_CATALOG` keeps the old one, leaving the
    process in a state no real run can reach — and leaking that divergence into
    every test that follows.
    """
    from aether.config import session as session_mod

    before = len(PROVIDER_CATALOG)
    session_mod._extend_catalog()
    session_mod._extend_catalog()
    assert len(PROVIDER_CATALOG) == before, "extending twice duplicated entries"

    ids = [c["id"] for c in PROVIDER_CATALOG]
    assert len(ids) == len(set(ids)), "duplicate catalogue ids"


# --- the readiness column ------------------------------------------------


def test_api_readiness_follows_the_key():
    from aether.config.providers import get, is_ready

    deepseek = get("deepseek")
    assert not is_ready(deepseek, env={})
    assert is_ready(deepseek, env={"DEEPSEEK_API_KEY": "sk-x"})
    assert not is_ready(deepseek, env={"OPENAI_API_KEY": "sk-x"}), "wrong key counted as ready"


def test_cli_readiness_follows_the_path(monkeypatch):
    import shutil

    from aether.config.providers import get, is_ready

    # is_ready imports shutil inside the function, so patching the module works
    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/aider" if cmd == "aider" else None)
    assert is_ready(get("aider"))
    assert not is_ready(get("codex_cli"))


def test_local_readiness_requires_something_listening():
    """The bug this replaced: every local runtime showed a green tick always."""
    import socket

    from aether.config.providers import get, is_ready, local_endpoint_is_up

    assert not local_endpoint_is_up("http://127.0.0.1:1")

    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        assert local_endpoint_is_up(f"http://127.0.0.1:{port}")

    # nothing is listening on LM Studio's port in CI, so it must not claim ready
    lmstudio = get("lmstudio")
    if not local_endpoint_is_up(lmstudio.base_url):
        assert not is_ready(lmstudio)


def test_local_probe_survives_a_junk_url():
    from aether.config.providers import local_endpoint_is_up

    assert not local_endpoint_is_up("not-a-url")
    assert not local_endpoint_is_up("")


def test_generic_cli_is_not_offered_as_a_choice():
    """It has no command, so it would render as a broken install."""
    from aether.config.providers import selectable

    assert "generic_cli" not in {p.id for p in selectable()}


# --- config rendering ----------------------------------------------------


def test_generated_config_is_disabled_by_default():
    """Listing a provider must never quietly turn it on."""
    for p in ALL_PROVIDERS:
        entry = to_provider_config(p)
        assert entry["enabled"] is False, f"{p.id} would be enabled just by existing"


def test_generated_config_loads_as_a_provider():
    """Round-trip through the real model, so a bad field is caught here."""
    from aether.domain.models import ProviderConfig

    for p in ALL_PROVIDERS:
        if p.id == "generic_cli":
            continue
        entry = to_provider_config(p, enabled=True)
        cfg = ProviderConfig(provider_id=p.id, **entry)
        assert cfg.provider_id == p.id
        if p.kind == "cli":
            assert cfg.command == p.command
        else:
            assert str(cfg.base_url).rstrip("/") == p.base_url.rstrip("/")
