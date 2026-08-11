"""Loopback token auth for the local API.

The API can create tasks (which spawn subprocesses), store provider API keys and
grant sandbox operations, and it has no user model. Binding to 127.0.0.1 keeps
remote hosts out but does nothing about other local processes, or about a web
page in the user's browser issuing cross-site requests. A shared bearer token
closes both.

The token lives in ``data/api_token`` (created on first run, 0600 where the OS
supports it). Clients pass it as ``Authorization: Bearer <token>``, an
``X-Aether-Token`` header, or — for WebSocket/EventSource, which cannot set
headers — a ``token`` query parameter.
"""

from __future__ import annotations

import hmac
import os
import secrets
import stat
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request, WebSocket

# Endpoints reachable without a token: liveness only. Nothing here exposes
# state, drives an agent, or accepts input.
PUBLIC_PATHS = {"/api/health", "/docs", "/openapi.json", "/redoc"}

# Only the API surfaces are gated. The built dashboard (static JS/CSS/HTML)
# holds no data — every byte it displays comes from a token-gated call.
#
# `/v1/` is the OpenAI-compatible gateway. It must be gated for a stronger
# reason than the dashboard API: it spends real money. An unauthenticated
# /v1/chat/completions on loopback is an open relay to every provider key on
# the machine, reachable by any local process.
GUARDED_PREFIXES = ("/api/", "/v1/")

# Back-compat for anything importing the old name.
GUARDED_PREFIX = "/api/"

TOKEN_FILENAME = "api_token"


def token_path(data_dir: Path) -> Path:
    return Path(data_dir) / TOKEN_FILENAME


def load_or_create_token(data_dir: Path) -> str:
    """Return the persistent API token, generating one on first use."""
    env_token = os.environ.get("AETHER_API_TOKEN")
    if env_token:
        return env_token.strip()

    path = token_path(data_dir)
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # best effort: Windows ACLs are not chmod-shaped
        pass
    return token


def extract_token(
    *,
    headers: Optional[dict] = None,
    query_token: Optional[str] = None,
) -> Optional[str]:
    headers = headers or {}
    authorization = headers.get("authorization") or headers.get("Authorization")
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    header_token = headers.get("x-aether-token") or headers.get("X-Aether-Token")
    if header_token:
        return header_token.strip()
    if query_token:
        return query_token.strip()
    return None


def token_matches(expected: str, presented: Optional[str]) -> bool:
    if not presented:
        return False
    return hmac.compare_digest(expected, presented)


def check_request(request: Request, expected: str) -> None:
    """Raise 401 unless the request carries the API token."""
    path = request.url.path
    if not path.startswith(GUARDED_PREFIXES):
        return
    if path in PUBLIC_PATHS or request.method == "OPTIONS":
        return
    presented = extract_token(
        headers=dict(request.headers),
        query_token=request.query_params.get("token"),
    )
    if not token_matches(expected, presented):
        raise HTTPException(
            status_code=401,
            detail=(
                "Missing or invalid API token. Pass 'Authorization: Bearer <token>' "
                "or '?token=<token>'. The token is printed by `aether serve` and "
                "stored in data/api_token."
            ),
        )


def check_websocket(ws: WebSocket, expected: str) -> bool:
    presented = extract_token(
        headers=dict(ws.headers),
        query_token=ws.query_params.get("token"),
    )
    return token_matches(expected, presented)
