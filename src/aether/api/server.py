"""FastAPI HTTP + WebSocket API."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from aether import __version__
from aether.api.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_SECONDS,
    check_request,
    check_websocket,
    create_session_cookie,
    extract_token,
    load_or_create_token,
    token_matches,
    validate_token,
)
from aether.app import AetherApp
from aether.core.doctor import run_doctor
from aether.domain.enums import ApprovalLevel, TaskMode, TaskStatus
from aether.domain.models import utcnow


class TaskCreateBody(BaseModel):
    prompt: str
    mode: TaskMode = TaskMode.SOLO
    approval_level: ApprovalLevel | None = None
    auto_start: bool = True
    priority: int = 50
    max_tokens: int | None = None
    max_requests: int | None = None
    max_cost_usd: float | None = None


class ApprovalBody(BaseModel):
    operation: str


class ResolveApprovalBody(BaseModel):
    granted: bool = True
    resolved_by: str = "user"
    auto_resume: bool = True


class SandboxGrantBody(BaseModel):
    operation: str


class ConnectBody(BaseModel):
    force: bool = False
    api_key: str | None = None


class ApiKeyBody(BaseModel):
    api_key: str = Field(..., min_length=1)


class OpenAIMessage(BaseModel):
    role: str
    content: Any = ""


class OpenAIChatRequest(BaseModel):
    """The subset of the OpenAI chat-completions request AETHER acts on.

    Extra fields are accepted and ignored rather than rejected: real clients
    send temperature, top_p, tool definitions and vendor extensions, and a 422
    on an unknown key would make AETHER useless as a drop-in endpoint.
    """

    model_config = {"extra": "allow"}

    model: str = "auto"
    messages: list[OpenAIMessage] = Field(default_factory=list)
    stream: bool = False


class ChatSessionCreate(BaseModel):
    provider_id: str | None = None
    title: str = "AETHER chat"
    mode: str = "chat"


class ChatMessageBody(BaseModel):
    message: str = Field(..., min_length=1)
    stream: bool = True


class DebateBody(BaseModel):
    topic: str = Field(..., min_length=1)
    rounds: int | None = None
    session_id: str | None = None


def create_fastapi_app(application: AetherApp) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await application.start()
        try:
            yield
        finally:
            await application.stop()

    api = FastAPI(
        title="AETHER",
        version=__version__,
        description="Multi-Agent AI Orchestrator",
        lifespan=lifespan,
    )

    # NOTE: no wildcard origin here. `allow_origins=[..., "*"]` with
    # allow_credentials=True lets any page in the user's browser drive this API.
    api.add_middleware(
        CORSMiddleware,
        allow_origins=application.settings.server.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    server_settings = application.settings.server
    api_token = ""
    if server_settings.require_auth:
        api_token = validate_token(
            server_settings.api_token
            or load_or_create_token(Path(application.settings.data_dir))
        )

        @api.middleware("http")
        async def _require_token(request: Request, call_next):
            try:
                check_request(request, api_token)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            # Cookie authentication is convenient for the dashboard, but state
            # changes must still come from this origin or an explicitly trusted
            # development origin. Bearer/header clients are not subject to CSRF.
            has_valid_header_token = token_matches(
                api_token, extract_token(headers=dict(request.headers))
            )
            origin = request.headers.get("origin")
            if (
                request.method not in {"GET", "HEAD", "OPTIONS"}
                and request.cookies.get(SESSION_COOKIE)
                and not has_valid_header_token
                and origin
            ):
                same_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
                allowed = {same_origin, *server_settings.cors_origins}
                if origin.rstrip("/") not in {item.rstrip("/") for item in allowed}:
                    return JSONResponse({"detail": "Cross-origin cookie request rejected"}, 403)
            return await call_next(request)

    @api.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "object-src 'none'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self' ws: wss:",
        )
        if request.url.path.startswith(("/api/", "/v1/")):
            response.headers.setdefault("Cache-Control", "no-store")
        if request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    subscribers: list[WebSocket] = []

    async def broadcast(event: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in subscribers:
            try:
                await ws.send_json(event)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            if ws in subscribers:
                subscribers.remove(ws)

    application.orchestrator.on_event(lambda e: asyncio.create_task(broadcast(e)))

    @api.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    @api.post("/api/auth/session", status_code=204)
    async def create_dashboard_session(request: Request) -> Response:
        """Exchange the URL-fragment bootstrap token for an HttpOnly cookie."""
        response = Response(status_code=204)
        response.set_cookie(
            SESSION_COOKIE,
            create_session_cookie(api_token),
            max_age=SESSION_MAX_AGE_SECONDS,
            httponly=True,
            secure=request.url.scheme == "https" or server_settings.secure_cookies,
            samesite="strict",
            path="/",
        )
        return response

    @api.get("/api/doctor")
    async def doctor() -> dict[str, Any]:
        return await run_doctor(application)

    @api.get("/api/metrics")
    async def metrics() -> dict[str, Any]:
        return application.metrics.summary()

    @api.get("/api/metrics/tasks/{task_id}")
    async def task_metrics(task_id: str) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "trace": application.metrics.task_trace(task_id),
            "budget": application.budgets.snapshot(task_id),
            "events": application.events.history(task_id=task_id, limit=100),
        }

    @api.get("/api/events")
    async def events(task_id: str | None = None, type: str | None = None, limit: int = 100) -> list[dict]:
        return application.events.history(task_id=task_id, event_type=type, limit=limit)

    @api.get("/api/status")
    async def status() -> dict[str, Any]:
        tasks = await application.tasks.list(limit=100)
        health_list = await application.registry.health_all()
        active = [
            t
            for t in tasks
            if t.status
            in {
                TaskStatus.RUNNING,
                TaskStatus.PLANNING,
                TaskStatus.QUEUED,
                TaskStatus.HANDOFF,
                TaskStatus.TESTING,
                TaskStatus.WAITING,
            }
        ]
        return {
            "version": __version__,
            "task_count": len(tasks),
            "active_tasks": len(active),
            "pending_approvals": len(application.approvals.list_pending()),
            "metrics": application.metrics.summary(),
            "providers": [
                {
                    "id": h.provider_id,
                    "available": h.available,
                    "availability": h.availability.value,
                    "message": h.message,
                    "latency_ms": h.latency_ms,
                }
                for h in health_list
            ],
            "circuits": [c.model_dump(mode="json") for c in application.circuit.all_snapshots()],
        }

    @api.get("/api/providers")
    async def providers() -> list[dict[str, Any]]:
        return [cfg.model_dump(mode="json") for cfg in application.registry.list_configs()]

    @api.get("/api/agents/health")
    async def agents_health() -> list[dict[str, Any]]:
        health_list = await application.registry.health_all()
        return [h.model_dump(mode="json") for h in health_list]

    @api.get("/api/connect")
    async def connect_overview() -> dict[str, Any]:
        """Connection status for all known providers (Claude OAuth, API keys, CLIs)."""
        from aether.cli.connect import claude_auth_status, provider_connect_info
        from aether.config.session import (
            OPENCODE_FALLBACK_MODELS,
            PROVIDER_CATALOG,
            env_path,
            load_dotenv_file,
            load_profile,
            primary_login_providers,
        )

        data_dir = Path(application.settings.data_dir)
        load_dotenv_file(env_path(data_dir))
        providers = [provider_connect_info(c["id"]) for c in PROVIDER_CATALOG]
        # enrich catalog metadata for UI
        catalog = []
        for c in primary_login_providers():
            catalog.append(
                {
                    "id": c["id"],
                    "name": c["name"],
                    "family": c.get("family"),
                    "kind": c["kind"],
                    "connect": c.get("connect"),
                    "models": c.get("models") or [],
                    "default_model": c.get("default_model"),
                    "multi_model": bool(c.get("multi_model")),
                    "blurb": c.get("blurb"),
                    "api_key_env": c.get("api_key_env"),
                    "key_url": c.get("key_url"),
                }
            )
        claude = claude_auth_status()
        profile = load_profile(data_dir)
        return {
            "claude": {
                "loggedIn": bool(claude.get("loggedIn")),
                "email": claude.get("email"),
                "authMethod": claude.get("authMethod"),
                "subscriptionType": claude.get("subscriptionType"),
                "orgName": claude.get("orgName"),
            },
            "providers": providers,
            "catalog": catalog,
            "profile": {
                "mode": profile.mode.value,
                "selected_providers": profile.selected_providers,
                "models": profile.models,
                "opencode_models": profile.opencode_models,
            },
            "opencode_models_fallback": OPENCODE_FALLBACK_MODELS,
        }

    @api.get("/api/connect/opencode/models")
    async def opencode_models() -> dict[str, Any]:
        """List models available through Open Code CLI."""
        from aether.adapters.opencode_adapter import list_opencode_models, opencode_bin
        from aether.config.session import OPENCODE_FALLBACK_MODELS

        if not opencode_bin():
            return {
                "ok": False,
                "installed": False,
                "models": OPENCODE_FALLBACK_MODELS,
                "message": "opencode CLI not found — showing fallback catalog",
            }
        models = await list_opencode_models()
        return {
            "ok": True,
            "installed": True,
            "models": models or OPENCODE_FALLBACK_MODELS,
            "count": len(models or OPENCODE_FALLBACK_MODELS),
        }

    @api.post("/api/connect/opencode/select")
    async def opencode_select_models(body: dict[str, Any]) -> dict[str, Any]:
        """Save multi-model Open Code selection into user profile + enable virtual providers."""
        import yaml

        from aether.config.session import (
            UserProfile,
            apply_profile_to_settings_dict,
            env_path,
            load_dotenv_file,
            load_profile,
            save_profile,
        )
        from aether.config.settings import find_config_path, load_yaml_config, settings_from_dict

        models = body.get("models") or []
        if not isinstance(models, list) or not models:
            raise HTTPException(400, "body.models must be a non-empty list of opencode model ids")

        data_dir = Path(application.settings.data_dir)
        load_dotenv_file(env_path(data_dir))
        profile = load_profile(data_dir)
        selected = list(profile.selected_providers)
        if "opencode_cli" not in selected:
            selected.append("opencode_cli")
        profile = UserProfile(
            mode=profile.mode,
            selected_providers=selected,
            models={**profile.models, "opencode_cli": models[0]},
            opencode_models=[str(m) for m in models],
            setup_complete=True,
            prefer_cli=True,
            multi_roles=profile.multi_roles,
            last_prompt=profile.last_prompt,
        )
        save_profile(data_dir, profile)

        path = find_config_path()
        base = load_yaml_config(path) if path else {}
        merged = apply_profile_to_settings_dict(base, profile)
        merged["data_dir"] = str(data_dir)
        settings = settings_from_dict(merged)
        await application.registry.reconfigure_providers(settings.providers)

        # persist user yaml
        discovered = find_config_path()
        if discovered is not None and discovered.name == "aether.yaml":
            user_cfg = discovered
        else:
            user_cfg = Path.home() / ".aether" / "config.yaml"
        user_cfg.parent.mkdir(parents=True, exist_ok=True)
        safe = apply_profile_to_settings_dict(base, profile)
        safe["data_dir"] = "./data"
        safe["workspace_root"] = "./workspace"
        user_cfg.write_text(yaml.safe_dump(safe, sort_keys=False), encoding="utf-8")

        return {
            "ok": True,
            "opencode_models": profile.opencode_models,
            "enabled_providers": [
                p for p, cfg in settings.providers.items() if cfg.enabled
            ],
        }

    @api.get("/api/connect/{provider_id}")
    async def connect_status(provider_id: str) -> dict[str, Any]:
        from aether.cli.connect import provider_connect_info
        from aether.config.session import env_path, load_dotenv_file

        load_dotenv_file(env_path(Path(application.settings.data_dir)))
        if provider_id in ("claude", "anthropic"):
            provider_id = "claude_cli"
        return provider_connect_info(provider_id)

    @api.post("/api/connect/{provider_id}")
    async def connect_provider(
        provider_id: str, body: ConnectBody | None = None
    ) -> dict[str, Any]:
        """
        Connect a provider:
        - claude_cli: starts official browser OAuth (claude auth login)
        - openai/grok/gemini: opens key page; optional api_key in body
        """
        from aether.cli.connect import connect_provider_api
        from aether.config.session import env_path, load_dotenv_file

        body = body or ConnectBody()
        data_dir = Path(application.settings.data_dir)
        load_dotenv_file(env_path(data_dir))
        if provider_id in ("claude", "anthropic"):
            provider_id = "claude_cli"

        result = await asyncio.to_thread(
            connect_provider_api,
            provider_id,
            data_dir,
            force=body.force,
            api_key=body.api_key,
        )

        # If API key saved / provider enabled, re-init registry adapters
        if result.ok and body.api_key:
            cfg = application.registry.get_config(provider_id)
            if cfg:
                cfg.enabled = True
            # re-check health after key set
            adapter = application.registry.get(provider_id)
            if adapter is None and cfg:
                application.registry.register(
                    __import__("aether.adapters.api_adapter", fromlist=["ApiAgentAdapter"]).ApiAgentAdapter(cfg),
                    cfg,
                )
                adapter = application.registry.get(provider_id)
            if adapter:
                await adapter.initialize()

        if provider_id == "claude_cli" and result.ok:
            # enable claude in registry config when connected
            cfg = application.registry.get_config("claude_cli")
            if cfg:
                cfg.enabled = True

        await broadcast(
            {
                "type": "provider_connect",
                "provider_id": result.provider_id,
                "ok": result.ok,
                "method": result.method,
                "message": result.message,
            }
        )
        return {
            "provider_id": result.provider_id,
            "ok": result.ok,
            "method": result.method,
            "message": result.message,
            "detail": result.detail,
        }

    @api.post("/api/connect/{provider_id}/key")
    async def connect_save_key(provider_id: str, body: ApiKeyBody) -> dict[str, Any]:
        """Save API key for OpenAI/Grok/Gemini-style providers."""
        from aether.adapters.api_adapter import ApiAgentAdapter
        from aether.cli.connect import save_api_key
        from aether.config.session import env_path, load_dotenv_file

        data_dir = Path(application.settings.data_dir)
        load_dotenv_file(env_path(data_dir))
        if provider_id in ("claude", "anthropic", "claude_cli"):
            raise HTTPException(
                400,
                "Claude uses browser OAuth. POST /api/connect/claude_cli instead of pasting a key.",
            )
        result = save_api_key(provider_id, body.api_key, data_dir)
        if not result.ok:
            raise HTTPException(400, result.message)

        cfg = application.registry.get_config(provider_id)
        if cfg:
            cfg.enabled = True
            if application.registry.get(provider_id) is None:
                adapter = ApiAgentAdapter(cfg)
                await adapter.initialize()
                application.registry.register(adapter, cfg)
            else:
                await application.registry.get(provider_id).initialize()  # type: ignore[union-attr]

        await broadcast(
            {
                "type": "provider_connect",
                "provider_id": provider_id,
                "ok": True,
                "method": "api_key",
                "message": result.message,
            }
        )
        return {
            "provider_id": provider_id,
            "ok": True,
            "method": "api_key",
            "message": result.message,
        }

    # ---------- OpenAI-compatible gateway ----------
    #
    # Lets any tool that speaks OpenAI — Cursor, Cline, Aider, Continue, the
    # openai SDK — point at AETHER and get its failover, compaction and budget
    # gates underneath, without knowing AETHER exists. Set base_url to
    # http://127.0.0.1:7420/v1 and the API token as the key.
    #
    # Deliberately stateless, because the protocol is: clients resend the whole
    # history each turn, so a server-side session keyed on nothing would either
    # duplicate that history or contradict it.
    #
    # `/v1/` is token-gated by the same middleware as `/api/` — see auth.py.
    # An open one would be a relay to every provider key on the machine.

    def _openai_error(status: int, message: str, err_type: str) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content={"error": {"message": message, "type": err_type, "code": None}},
        )

    @api.get("/v1/models")
    async def openai_list_models() -> dict[str, Any]:
        """Enabled providers, in the shape an OpenAI client expects."""
        created = int(utcnow().timestamp())
        data = [
            {"id": cfg.provider_id, "object": "model", "created": created, "owned_by": "aether"}
            for cfg in application.registry.list_configs()
            if cfg.enabled
        ]
        data.insert(0, {"id": "auto", "object": "model", "created": created, "owned_by": "aether"})
        return {"object": "list", "data": data}

    @api.post("/v1/chat/completions")
    async def openai_chat_completions(body: OpenAIChatRequest) -> Any:
        import uuid

        from aether.core.chat_session import ChatMessage

        if body.stream:
            # Better an explicit, correctly-shaped refusal than a malformed SSE
            # body that hangs the client. Non-streaming is the whole protocol
            # for most callers; wiring stream=true is tracked as WO-26.
            return _openai_error(
                400,
                "AETHER's OpenAI gateway does not support stream=true yet. "
                "Retry with stream=false.",
                "invalid_request_error",
            )

        user_turns = [m for m in body.messages if m.role == "user"]
        if not user_turns:
            return _openai_error(400, "messages must contain a user turn",
                                 "invalid_request_error")

        requested = (body.model or "auto").strip()
        provider_id = None if requested in ("", "auto", "aether") else requested
        if provider_id and application.registry.get(provider_id) is None:
            return _openai_error(404, f"Unknown model '{provider_id}'. "
                                      f"See GET /v1/models.", "invalid_request_error")

        session = application.chat_router.create_session(
            provider_id=provider_id, title="openai-gateway"
        )
        # Replay everything before the final user turn as history, then send
        # that turn — chat() appends it itself.
        last = user_turns[-1]
        for message in body.messages:
            if message is last:
                break
            content = message.content if isinstance(message.content, str) else str(message.content)
            session.append(ChatMessage(role=message.role, content=content))

        text = last.content if isinstance(last.content, str) else str(last.content)
        result = await application.chat_router.chat(session, text)

        if not result.get("ok"):
            return _openai_error(502, result.get("error") or "all providers failed",
                                 "upstream_error")

        reply = result.get("reply") or ""
        usage = result.get("usage") or {}
        used = result.get("provider_id") or "unknown"
        # Mirrors the routing transparency the timeline now carries: the caller
        # can see which provider actually answered and whether it failed over.
        failovers = [e for e in (result.get("events") or []) if "failover" in e]
        return JSONResponse(
            content={
                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                "object": "chat.completion",
                "created": int(utcnow().timestamp()),
                "model": used,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": reply},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                },
            },
            headers={
                "X-Aether-Provider": used,
                "X-Aether-Failovers": str(len(failovers)),
            },
        )

    # ---------- Chat / LLM router sessions ----------
    @api.get("/api/chat/sessions")
    async def list_chat_sessions(limit: int = 30) -> list[dict[str, Any]]:
        return application.chat_store.list_recent(limit=limit)

    @api.post("/api/chat/sessions")
    async def create_chat_session(body: ChatSessionCreate) -> dict[str, Any]:
        session = application.chat_router.create_session(
            provider_id=body.provider_id,
            mode=body.mode,
            title=body.title,
        )
        return session.model_dump(mode="json")

    @api.get("/api/chat/sessions/{session_id}")
    async def get_chat_session(session_id: str) -> dict[str, Any]:
        session = application.chat_store.load(session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        from aether.core.context_budget import ContextBudget, context_window_for

        active = session.active_provider or "mock"
        cfg = application.registry.get_config(active)
        window = context_window_for(
            active, config_window=cfg.context_window if cfg else None
        )
        budget = ContextBudget(
            context_window=window,
            compaction_ratio=application.settings.orchestrator.context_compaction_ratio,
        )
        usage = budget.usage(session.messages_as_dicts(), summary=session.memory_summary)
        data = session.model_dump(mode="json")
        data["usage"] = usage
        return data

    @api.post("/api/chat/sessions/{session_id}/messages")
    async def chat_message(session_id: str, body: ChatMessageBody) -> dict[str, Any]:
        session = application.chat_store.load(session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        if body.stream:
            # non-stream callers still get full result; streaming uses /stream or WS
            pass
        result = await application.chat_router.chat(
            session,
            body.message,
            max_failovers=application.settings.orchestrator.chat_max_failovers,
        )
        # strip heavy session object for JSON
        out = {k: v for k, v in result.items() if k != "session"}
        out["session"] = result["session"].model_dump(mode="json")
        await broadcast(
            {
                "type": "chat_message",
                "session_id": session_id,
                "ok": result.get("ok"),
                "provider_id": result.get("provider_id"),
            }
        )
        return out

    @api.post("/api/chat/sessions/{session_id}/stream")
    async def chat_stream(session_id: str, body: ChatMessageBody):
        """SSE stream of tokens for live chat."""
        import json as _json

        from fastapi.responses import StreamingResponse

        session = application.chat_store.load(session_id)
        if not session:
            raise HTTPException(404, "Session not found")

        async def event_gen():
            async for ev in application.chat_router.chat_stream(
                session,
                body.message,
                max_failovers=application.settings.orchestrator.chat_max_failovers,
            ):
                yield f"data: {_json.dumps(ev, default=str)}\n\n"
                if ev.get("type") in {"done", "error"} and ev.get("type") == "done":
                    await broadcast(
                        {
                            "type": "chat_stream_done",
                            "session_id": session_id,
                            "ok": ev.get("ok"),
                            "provider_id": ev.get("provider_id"),
                        }
                    )

        return StreamingResponse(event_gen(), media_type="text/event-stream")

    @api.post("/api/chat/sessions/{session_id}/compact")
    async def chat_compact(session_id: str) -> dict[str, Any]:
        session = application.chat_store.load(session_id)
        if not session:
            raise HTTPException(404, "Session not found")
        ok = await application.chat_router.maybe_compact(
            session, force=True, reason="api_compact"
        )
        return {
            "ok": ok,
            "session_id": session_id,
            "segment": session.ensure_segment().index,
            "summary": session.memory_summary[:2000],
        }

    @api.post("/api/chat/debate")
    async def chat_debate(body: DebateBody) -> dict[str, Any]:
        session = None
        if body.session_id:
            session = application.chat_store.load(body.session_id)
        result = await application.debate_engine.run(
            body.topic, rounds=body.rounds, session=session
        )
        if result.get("session"):
            result = dict(result)
            result["session"] = result["session"].model_dump(mode="json")
        return result

    @api.post("/api/tasks")
    async def create_task(body: TaskCreateBody) -> dict[str, Any]:
        task = await application.orchestrator.submit(
            body.prompt,
            mode=body.mode,
            auto_start=body.auto_start,
            approval_level=body.approval_level,
            priority=body.priority,
            max_tokens=body.max_tokens,
            max_requests=body.max_requests,
            max_cost_usd=body.max_cost_usd,
        )
        return task.model_dump(mode="json")

    @api.get("/api/tasks")
    async def list_tasks(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        st = TaskStatus(status) if status else None
        tasks = await application.tasks.list(status=st, limit=limit)
        return [t.model_dump(mode="json") for t in tasks]

    @api.get("/api/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        try:
            return await application.orchestrator.inspect(task_id)
        except KeyError:
            raise HTTPException(404, "Task not found") from None

    @api.post("/api/tasks/{task_id}/pause")
    async def pause_task(task_id: str) -> dict[str, Any]:
        try:
            t = await application.orchestrator.pause_task(task_id)
            return t.model_dump(mode="json")
        except KeyError:
            raise HTTPException(404, "Task not found") from None

    @api.post("/api/tasks/{task_id}/resume")
    async def resume_task(task_id: str) -> dict[str, Any]:
        try:
            t = await application.orchestrator.resume_task(task_id)
            return t.model_dump(mode="json")
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict[str, Any]:
        try:
            t = await application.orchestrator.cancel_task(task_id)
            return t.model_dump(mode="json")
        except KeyError:
            raise HTTPException(404, "Task not found") from None

    @api.get("/api/tasks/{task_id}/export")
    async def export_task(task_id: str) -> dict[str, Any]:
        try:
            return await application.exporter.export_dict(task_id)
        except KeyError:
            raise HTTPException(404, "Task not found") from None

    @api.post("/api/tasks/import")
    async def import_task(body: dict[str, Any]) -> dict[str, Any]:
        try:
            task = await application.exporter.import_dict(body, as_paused=True)
            return task.model_dump(mode="json")
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.get("/api/approvals")
    async def list_approvals(task_id: str | None = None, pending: bool = True) -> list[dict]:
        if pending:
            return [a.model_dump() for a in application.approvals.list_pending(task_id=task_id)]
        return [a.model_dump() for a in application.approvals.list_all()]

    @api.post("/api/approvals/{approval_id}/resolve")
    async def resolve_approval(approval_id: str, body: ResolveApprovalBody) -> dict[str, Any]:
        try:
            if body.granted:
                req = application.approvals.get(approval_id)
                if req:
                    application.sandbox.grant(req.operation)
            return await application.orchestrator.resolve_approval(
                approval_id,
                granted=body.granted,
                resolved_by=body.resolved_by,
                auto_resume=body.auto_resume,
            )
        except KeyError:
            raise HTTPException(404, "Approval not found") from None

    @api.post("/api/sandbox/grant")
    async def sandbox_grant(body: SandboxGrantBody) -> dict[str, str]:
        application.sandbox.grant(body.operation)
        return {"status": "granted", "operation": body.operation}

    @api.post("/api/approvals")
    async def grant_approval_legacy(body: ApprovalBody) -> dict[str, str]:
        """Backward-compatible sandbox grant endpoint."""
        application.sandbox.grant(body.operation)
        return {"status": "granted", "operation": body.operation}

    @api.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        if server_settings.require_auth and not check_websocket(ws, api_token):
            await ws.close(code=4401, reason="invalid or missing API token")
            return
        await ws.accept()
        subscribers.append(ws)
        try:
            await ws.send_json({"type": "connected", "version": __version__})
            # push recent events snapshot
            for event in application.events.history(limit=20):
                await ws.send_json(event)
            while True:
                data = await ws.receive_text()
                if data == "ping":
                    await ws.send_json({"type": "pong"})
                elif data.startswith("subscribe:"):
                    # client can filter; still receives all for simplicity
                    await ws.send_json({"type": "subscribed", "filter": data.split(":", 1)[-1]})
        except WebSocketDisconnect:
            pass
        finally:
            if ws in subscribers:
                subscribers.remove(ws)

    # Serve the built dashboard from the API itself, so the normal workflow
    # needs no Vite dev server (which is where every current npm advisory
    # lives) and the UI shares an origin with the API — no CORS, no proxy.
    # Mounted last so it never shadows an API route.
    dashboard = dashboard_dir(application.settings)
    if dashboard is not None:
        api.mount("/", StaticFiles(directory=str(dashboard), html=True), name="dashboard")

    return api


def dashboard_dir(settings: Any) -> Path | None:
    """Location of the built dashboard, if it has been built."""
    configured = getattr(settings.server, "static_dir", "") or ""
    if configured:
        path = Path(configured).expanduser()
        return path if (path / "index.html").exists() else None
    # Installed wheel: assets live inside the package.
    packaged = Path(__file__).resolve().parents[1] / "resources" / "dashboard"
    if (packaged / "index.html").exists():
        return packaged
    # Source checkout: src/aether/api/server.py -> <repo>/web/dist
    source = Path(__file__).resolve().parents[3] / "web" / "dist"
    return source if (source / "index.html").exists() else None
