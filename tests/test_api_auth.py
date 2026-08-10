"""The local API must not be drivable by any local process or browser page."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aether.api.auth import load_or_create_token, token_path
from aether.api.server import create_fastapi_app
from aether.app import AetherApp


@pytest.fixture
def client(app: AetherApp) -> TestClient:
    return TestClient(create_fastapi_app(app))


def _token(app: AetherApp) -> str:
    return load_or_create_token(Path(app.settings.data_dir))


def test_health_is_public(client: TestClient):
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_unauthenticated_requests_are_rejected(client: TestClient):
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/tasks").status_code == 401
    assert client.post("/api/tasks", json={"prompt": "do a thing"}).status_code == 401
    # the endpoints that matter most: key storage and sandbox grants
    assert client.post("/api/connect/openai/key", json={"api_key": "sk-x"}).status_code == 401
    assert client.post("/api/sandbox/grant", json={"operation": "install"}).status_code == 401


def test_bearer_token_is_accepted(client: TestClient, app: AetherApp):
    res = client.get("/api/tasks", headers={"Authorization": f"Bearer {_token(app)}"})
    assert res.status_code == 200


def test_header_token_is_accepted(client: TestClient, app: AetherApp):
    res = client.get("/api/tasks", headers={"X-Aether-Token": _token(app)})
    assert res.status_code == 200


def test_query_token_is_accepted_for_header_less_clients(client: TestClient, app: AetherApp):
    res = client.get(f"/api/tasks?token={_token(app)}")
    assert res.status_code == 200


def test_wrong_token_is_rejected(client: TestClient):
    res = client.get("/api/tasks", headers={"Authorization": "Bearer not-the-token"})
    assert res.status_code == 401


def test_websocket_requires_token(client: TestClient, app: AetherApp):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws") as ws:
        ws.receive_json()

    with client.websocket_connect(f"/ws?token={_token(app)}") as ws:
        assert ws.receive_json()["type"] == "connected"


def test_cors_does_not_allow_arbitrary_origins(app: AetherApp):
    api = create_fastapi_app(app)
    origins = None
    for middleware in api.user_middleware:
        if middleware.cls.__name__ == "CORSMiddleware":
            origins = middleware.kwargs.get("allow_origins")
    assert origins is not None
    assert "*" not in origins, "wildcard origin with credentials lets any page drive the API"


def test_token_is_persisted_and_reused(app: AetherApp):
    data_dir = Path(app.settings.data_dir)
    first = load_or_create_token(data_dir)
    assert token_path(data_dir).exists()
    assert load_or_create_token(data_dir) == first


def test_auth_can_be_disabled_deliberately(app: AetherApp):
    app.settings.server.require_auth = False
    try:
        with TestClient(create_fastapi_app(app)) as unguarded:
            assert unguarded.get("/api/tasks").status_code == 200
    finally:
        app.settings.server.require_auth = True


# --- built dashboard is served by the API itself --------------------------


def test_static_dashboard_is_public_but_api_stays_gated(app: AetherApp, tmp_path: Path):
    """Serving the SPA from the API removes the need for a Vite dev server."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>aether</html>", encoding="utf-8")
    (dist / "app.js").write_text("console.log(1)", encoding="utf-8")
    app.settings.server.static_dir = str(dist)
    try:
        with TestClient(create_fastapi_app(app)) as client:
            # the shell and its assets load without a token...
            assert client.get("/").status_code == 200
            assert "aether" in client.get("/").text
            assert client.get("/app.js").status_code == 200
            # ...but every byte of data still needs one
            assert client.get("/api/tasks").status_code == 401
            assert client.get("/api/status").status_code == 401
            assert (
                client.get("/api/tasks", headers={"Authorization": f"Bearer {_token(app)}"}).status_code
                == 200
            )
    finally:
        app.settings.server.static_dir = ""


def test_no_static_mount_when_dashboard_not_built(app: AetherApp, tmp_path: Path):
    app.settings.server.static_dir = str(tmp_path / "does-not-exist")
    try:
        with TestClient(create_fastapi_app(app)) as client:
            assert client.get("/").status_code == 404
            assert client.get("/api/health").status_code == 200
    finally:
        app.settings.server.static_dir = ""
