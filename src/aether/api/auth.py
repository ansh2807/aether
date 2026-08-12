"""Loopback token auth for the local API.

The API can create tasks (which spawn subprocesses), store provider API keys and
grant sandbox operations, and it has no user model. Binding to 127.0.0.1 keeps
remote hosts out but does nothing about other local processes, or about a web
page in the user's browser issuing cross-site requests. A shared bearer token
closes both.

The token lives in ``data/api_token`` (created on first run, 0600 where the OS
supports it). Clients pass it as ``Authorization: Bearer <token>``, an
``X-Aether-Token`` header, or an HttpOnly dashboard session cookie. A ``token``
query parameter is accepted only for WebSockets, whose browser API cannot set
headers; ordinary HTTP query strings are deliberately rejected because they
leak into access logs, history, and referrers.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
import time
from pathlib import Path

from fastapi import HTTPException, Request, WebSocket

# Endpoints reachable without a token: liveness only. Nothing here exposes
# state, drives an agent, or accepts input.
PUBLIC_PATHS = {"/api/health"}
GUARDED_PATHS = {"/docs", "/openapi.json", "/redoc"}

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
SESSION_COOKIE = "aether_session"
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60
MIN_TOKEN_LENGTH = 32


def token_path(data_dir: Path) -> Path:
    return Path(data_dir) / TOKEN_FILENAME


def validate_token(token: str) -> str:
    token = token.strip()
    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(
            f"AETHER API token must contain at least {MIN_TOKEN_LENGTH} characters; "
            "set AETHER_API_TOKEN to a strong random value or remove data/api_token "
            "to regenerate it"
        )
    return token


def load_or_create_token(data_dir: Path) -> str:
    """Return the persistent API token, generating one on first use."""
    env_token = os.environ.get("AETHER_API_TOKEN")
    if env_token:
        return validate_token(env_token)

    path = token_path(data_dir)
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return validate_token(existing)

    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another process won first-start token creation. Its token is the
        # authority; never overwrite it with a different value.
        for _ in range(50):
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return validate_token(existing)
            time.sleep(0.01)
        raise RuntimeError(f"API token file was created but remained empty: {path}")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # best effort: Windows ACLs are not chmod-shaped
        pass
    return validate_token(token)


def extract_token(
    *,
    headers: dict | None = None,
    query_token: str | None = None,
) -> str | None:
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


def token_matches(expected: str, presented: str | None) -> bool:
    if not presented:
        return False
    return hmac.compare_digest(expected, presented)


def create_session_cookie(expected: str, now: int | None = None) -> str:
    issued = int(time.time()) if now is None else now
    message = str(issued).encode("ascii")
    signature = hmac.new(expected.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"{issued}.{signature}"


def session_cookie_matches(expected: str, presented: str | None, now: int | None = None) -> bool:
    if not presented:
        return False
    try:
        issued_raw, _signature = presented.split(".", 1)
        issued = int(issued_raw)
    except (TypeError, ValueError):
        return False
    current = int(time.time()) if now is None else now
    if issued > current + 60 or current - issued > SESSION_MAX_AGE_SECONDS:
        return False
    expected_cookie = create_session_cookie(expected, now=issued)
    return hmac.compare_digest(expected_cookie, presented)


def check_request(request: Request, expected: str) -> None:
    """Raise 401 unless the request carries the API token."""
    path = request.url.path
    if not path.startswith(GUARDED_PREFIXES) and path not in GUARDED_PATHS:
        return
    if path in PUBLIC_PATHS or request.method == "OPTIONS":
        return
    presented = extract_token(
        headers=dict(request.headers),
    )
    cookie_ok = session_cookie_matches(expected, request.cookies.get(SESSION_COOKIE))
    if not token_matches(expected, presented) and not cookie_ok:
        raise HTTPException(
            status_code=401,
            detail=(
                "Missing or invalid API token. Pass 'Authorization: Bearer <token>' "
                "or use the dashboard session link printed by `aether serve`. "
                "The token is "
                "stored in data/api_token."
            ),
        )


def check_websocket(ws: WebSocket, expected: str) -> bool:
    presented = extract_token(
        headers=dict(ws.headers),
        query_token=ws.query_params.get("token"),
    )
    return token_matches(expected, presented) or session_cookie_matches(
        expected, ws.cookies.get(SESSION_COOKIE)
    )
