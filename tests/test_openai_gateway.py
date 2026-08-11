"""The OpenAI-compatible gateway: `/v1/chat/completions`.

Point Cursor, Cline, Aider, Continue or the `openai` SDK at
`http://127.0.0.1:7420/v1` with the API token as the key, and they get AETHER's
failover, compaction and budget gates underneath without knowing AETHER exists.
That is the one thing a gateway like OmniRoute does that AETHER could not: it is
an endpoint, so existing tools work with it unchanged.

The first test here is the important one. `/v1/` spends real money, so an
unauthenticated route would be an open relay to every provider key on the
machine, reachable by any local process. The auth middleware only guarded
`/api/` before this endpoint existed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aether.api.server import create_fastapi_app


@pytest.fixture
def client(settings):
    settings.server.require_auth = True
    settings.server.api_token = "test-token-123"
    from aether.app import AetherApp

    app = AetherApp(settings)
    with TestClient(create_fastapi_app(app)) as c:
        yield c


AUTH = {"Authorization": "Bearer test-token-123"}


# --- the security property ----------------------------------------------


def test_the_gateway_is_not_an_open_relay(client):
    """Without this, any local process could spend the operator's API credits."""
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401, (
        f"/v1/chat/completions answered {resp.status_code} with no token — "
        "it is an unauthenticated relay to every provider key on this machine"
    )


def test_the_model_list_is_gated_too(client):
    """It enumerates the operator's configured providers."""
    assert client.get("/v1/models").status_code == 401


def test_a_wrong_token_is_rejected(client):
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer wrong"},
        json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401


# --- protocol shape ------------------------------------------------------


def test_a_completion_comes_back_in_openai_shape(client):
    resp = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"model": "auto", "messages": [{"role": "user", "content": "say hello"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert isinstance(body["created"], int)
    assert body["choices"][0]["index"] == 0
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert isinstance(body["choices"][0]["message"]["content"], str)
    assert body["choices"][0]["finish_reason"] == "stop"
    assert set(body["usage"]) >= {"prompt_tokens", "completion_tokens", "total_tokens"}


def test_the_response_names_the_provider_that_answered(client):
    """`model` echoing the request would hide a failover. It reports reality."""
    resp = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
    )
    body = resp.json()
    assert body["model"] != "auto", "the response did not say who actually answered"
    assert resp.headers["X-Aether-Provider"] == body["model"]
    assert resp.headers["X-Aether-Failovers"].isdigit()


def test_unknown_extra_fields_are_tolerated(client):
    """Real clients send temperature, tools and vendor extensions. A 422 on an
    unknown key would make AETHER useless as a drop-in endpoint."""
    resp = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": "auto",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.7,
            "top_p": 0.9,
            "presence_penalty": 0.0,
            "tools": [],
            "some_vendor_extension": {"nested": True},
        },
    )
    assert resp.status_code == 200, resp.text


def test_conversation_history_is_accepted(client):
    """The protocol is stateless: clients resend the whole history each turn."""
    resp = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": "auto",
            "messages": [
                {"role": "system", "content": "You are terse."},
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "second question"},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"]


def test_models_endpoint_lists_enabled_providers(client):
    resp = client.get("/v1/models", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    ids = [m["id"] for m in body["data"]]
    assert "auto" in ids, "'auto' must be offered — it is the routed default"
    assert "mock" in ids
    for entry in body["data"]:
        assert entry["object"] == "model" and entry["owned_by"] == "aether"


# --- errors, in the shape clients parse ---------------------------------


def test_streaming_is_refused_clearly_rather_than_half_done(client):
    """A malformed SSE body hangs the client. An explicit 400 does not."""
    resp = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"model": "auto", "messages": [{"role": "user", "content": "hi"}],
              "stream": True},
    )
    assert resp.status_code == 400
    assert "stream" in resp.json()["error"]["message"].lower()
    assert resp.json()["error"]["type"] == "invalid_request_error"


def test_an_unknown_model_is_a_404_not_a_silent_substitution(client):
    """Quietly answering from a different provider than the caller asked for is
    the worst option: they would never know."""
    resp = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"model": "no-such-provider", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 404
    assert "no-such-provider" in resp.json()["error"]["message"]


def test_no_user_turn_is_a_400(client):
    resp = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"model": "auto", "messages": [{"role": "system", "content": "hi"}]},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"


def test_errors_use_the_openai_error_envelope(client):
    """Clients parse `error.message`; a FastAPI `detail` string breaks them."""
    resp = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"model": "auto", "messages": []},
    )
    body = resp.json()
    assert "error" in body and "message" in body["error"], body
