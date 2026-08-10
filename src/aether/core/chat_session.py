"""Persistent multi-turn chat sessions with auto-summary rollover.

Core product loop:
  user ↔ active model (LLM router)
  on rate/quota/auth/context limit → switch model, keep memory
  on context soft-limit → summarize → new session segment → continue
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from aether.core.context_budget import (
    ContextBudget,
    context_window_for,
    estimate_tokens,
)
from aether.domain.models import new_id, utcnow


class ChatMessage(BaseModel):
    role: str  # user | assistant | system | model | summary
    content: str
    provider_id: Optional[str] = None
    model: Optional[str] = None
    timestamp: str = Field(default_factory=lambda: utcnow().isoformat())
    meta: dict[str, Any] = Field(default_factory=dict)


class SessionSegment(BaseModel):
    """One conversation segment (between roll-overs)."""

    segment_id: str = Field(default_factory=lambda: new_id("seg-"))
    index: int = 0
    summary_from_previous: str = ""
    messages: list[ChatMessage] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: utcnow().isoformat())
    closed_at: Optional[str] = None
    close_reason: Optional[str] = None  # compacted | switched | user


class ChatSession(BaseModel):
    session_id: str = Field(default_factory=lambda: new_id("chat-"))
    title: str = "AETHER chat"
    mode: str = "chat"  # chat | debate | task
    active_provider: Optional[str] = None
    participating_providers: list[str] = Field(default_factory=list)
    segments: list[SessionSegment] = Field(default_factory=list)
    # chain of rolling summaries across whole session
    memory_summary: str = ""
    created_at: str = Field(default_factory=lambda: utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: utcnow().isoformat())
    metadata: dict[str, Any] = Field(default_factory=dict)

    def ensure_segment(self) -> SessionSegment:
        if not self.segments:
            self.segments.append(SessionSegment(index=0, summary_from_previous=self.memory_summary))
        return self.segments[-1]

    def current_messages(self) -> list[ChatMessage]:
        return list(self.ensure_segment().messages)

    def append(self, msg: ChatMessage) -> None:
        seg = self.ensure_segment()
        seg.messages.append(msg)
        if msg.provider_id and msg.provider_id not in self.participating_providers:
            self.participating_providers.append(msg.provider_id)
        self.updated_at = utcnow().isoformat()

    def messages_as_dicts(self) -> list[dict[str, Any]]:
        return [m.model_dump() for m in self.current_messages()]


class ChatSessionStore:
    """JSON files under data/chat_sessions/."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.json"

    def save(self, session: ChatSession) -> Path:
        session.updated_at = utcnow().isoformat()
        p = self.path(session.session_id)
        p.write_text(session.model_dump_json(indent=2), encoding="utf-8")
        # index
        idx = self.root / "index.json"
        items: list[dict] = []
        if idx.exists():
            try:
                items = json.loads(idx.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                items = []
        items = [i for i in items if i.get("session_id") != session.session_id]
        items.insert(
            0,
            {
                "session_id": session.session_id,
                "title": session.title,
                "mode": session.mode,
                "active_provider": session.active_provider,
                "updated_at": session.updated_at,
                "segments": len(session.segments),
            },
        )
        idx.write_text(json.dumps(items[:200], indent=2), encoding="utf-8")
        return p

    def load(self, session_id: str) -> Optional[ChatSession]:
        p = self.path(session_id)
        if not p.exists():
            return None
        return ChatSession.model_validate_json(p.read_text(encoding="utf-8"))

    def list_recent(self, limit: int = 20) -> list[dict]:
        idx = self.root / "index.json"
        if not idx.exists():
            return []
        try:
            items = json.loads(idx.read_text(encoding="utf-8"))
            return items[:limit]
        except Exception:  # noqa: BLE001
            return []


class SessionCompactor:
    """Build summary text for rollover. Can use an LLM adapter when available."""

    def __init__(self, max_summary_chars: int = 6000) -> None:
        self.max_summary_chars = max_summary_chars

    def extractive_summary(
        self,
        messages: list[ChatMessage],
        previous_summary: str = "",
    ) -> str:
        """No-LLM fallback: structured extractive summary."""
        lines: list[str] = []
        if previous_summary:
            lines.append("## Carry-forward summary")
            lines.append(previous_summary[: self.max_summary_chars // 2])
            lines.append("")
        lines.append("## Conversation so far")
        user_pts: list[str] = []
        asst_pts: list[str] = []
        for m in messages:
            snippet = m.content.strip().replace("\n", " ")
            if len(snippet) > 280:
                snippet = snippet[:277] + "…"
            if m.role == "user":
                user_pts.append(f"- USER: {snippet}")
            elif m.role in ("assistant", "model"):
                who = m.provider_id or "assistant"
                asst_pts.append(f"- {who}: {snippet}")
        lines.append("### User goals / asks")
        lines.extend(user_pts[-12:] or ["- (none)"])
        lines.append("### Assistant / model replies")
        lines.extend(asst_pts[-12:] or ["- (none)"])
        lines.append("")
        lines.append("### Continuation instruction")
        lines.append(
            "Continue the work from this point. Do not restart from zero. "
            "Honor prior decisions and unfinished items."
        )
        text = "\n".join(lines)
        if len(text) > self.max_summary_chars:
            text = text[: self.max_summary_chars - 20] + "\n…[truncated]"
        return text

    async def summarize_with_adapter(
        self,
        adapter: Any,
        messages: list[ChatMessage],
        previous_summary: str,
        workspace: str,
    ) -> str:
        """Ask current model to compress chat; fall back to extractive."""
        from aether.domain.models import ExecutionRequest

        transcript = "\n".join(
            f"{m.role.upper()}({m.provider_id or '-'}): {m.content[:1500]}" for m in messages[-40:]
        )
        prompt = (
            "You are a session memory compressor for AETHER.\n"
            "Summarize the conversation so a NEW session can continue without loss.\n"
            "Include: goals, decisions, files/artifacts mentioned, open tasks, constraints.\n"
            "Be dense and factual. Max ~800 words.\n\n"
            f"PREVIOUS SUMMARY:\n{previous_summary or '(none)'}\n\n"
            f"TRANSCRIPT:\n{transcript}\n\n"
            "STATUS: COMPLETE\nCOMPLETED:\n- session summary\nREMAINING:\n- none\n"
            "RECOMMENDED_NEXT_ACTION:\ncontinue from summary\n"
        )
        try:
            result = await adapter.execute(
                ExecutionRequest(
                    task_id="SESSION-SUMMARY",
                    provider_id=getattr(adapter, "provider_id", "unknown"),
                    prompt=prompt,
                    workspace_path=workspace,
                    timeout_seconds=120,
                )
            )
            if result.success and result.output.strip():
                out = result.output.strip()
                if len(out) > self.max_summary_chars:
                    out = out[: self.max_summary_chars]
                return out
        except Exception:  # noqa: BLE001
            pass
        return self.extractive_summary(messages, previous_summary)


class ChatRouter:
    """
    LLM router for interactive chat:
    - pick active model
    - send message with session memory
    - on limit/failure → failover to next capable model
    - on context pressure → compact + new segment, continue
    """

    def __init__(
        self,
        app: Any,
        store: ChatSessionStore,
        compaction_ratio: float = 0.78,
        prefer_llm_summary: bool = True,
    ) -> None:
        self.app = app
        self.store = store
        self.compaction_ratio = compaction_ratio
        self.prefer_llm_summary = prefer_llm_summary
        self.compactor = SessionCompactor()

    def create_session(
        self,
        provider_id: Optional[str] = None,
        mode: str = "chat",
        title: str = "AETHER chat",
    ) -> ChatSession:
        providers = self._enabled_providers()
        active = provider_id if provider_id in providers else (providers[0] if providers else None)
        session = ChatSession(
            title=title,
            mode=mode,
            active_provider=active,
            participating_providers=[active] if active else [],
        )
        session.ensure_segment()
        self.store.save(session)
        return session

    def _enabled_providers(self) -> list[str]:
        registered = [
            cfg for cfg in self.app.registry.list_configs() if self.app.registry.get(cfg.provider_id)
        ]
        real = sorted(cfg.provider_id for cfg in registered if cfg.enabled and not cfg.for_testing)
        testing = sorted(
            cfg.provider_id for cfg in registered if cfg.for_testing and cfg.provider_id not in real
        )
        # A fake agent must not quietly answer for a real one that hit a limit.
        allow_testing = (
            getattr(self.app.settings.routing, "allow_testing_fallback", False) or not real
        )
        return real + testing if allow_testing else real

    def _budget_for(self, provider_id: Optional[str]) -> ContextBudget:
        cfg = self.app.registry.get_config(provider_id) if provider_id else None
        config_window = None
        if cfg:
            config_window = cfg.context_window
            if config_window is None and cfg.extra.get("context_window"):
                try:
                    config_window = int(cfg.extra["context_window"])
                except Exception:  # noqa: BLE001
                    config_window = None
            if config_window is None and isinstance(cfg.limits, dict) and cfg.limits.get("context_window"):
                try:
                    config_window = int(cfg.limits["context_window"])
                except Exception:  # noqa: BLE001
                    config_window = None
        window = context_window_for(provider_id or "mock", config_window=config_window)
        ratio = getattr(self.app.settings.orchestrator, "context_compaction_ratio", self.compaction_ratio)
        return ContextBudget(context_window=window, compaction_ratio=ratio)

    def _build_prompt(self, session: ChatSession, user_text: str) -> str:
        seg = session.ensure_segment()
        parts: list[str] = [
            "=== AETHER CHAT SESSION ===",
            f"Session: {session.session_id}",
            f"Segment: {seg.index}",
            f"Mode: {session.mode}",
            "",
        ]
        if session.memory_summary or seg.summary_from_previous:
            parts.append("=== CONTINUITY SUMMARY (prior context compacted) ===")
            parts.append(session.memory_summary or seg.summary_from_previous)
            parts.append("=== END SUMMARY — continue from here ===")
            parts.append("")
        # include recent messages in prompt for CLI/API adapters without native history
        recent = seg.messages[-20:]
        if recent:
            parts.append("=== RECENT MESSAGES ===")
            for m in recent:
                who = m.provider_id or m.role
                parts.append(f"{m.role.upper()}[{who}]: {m.content[:2000]}")
            parts.append("=== END RECENT ===")
            parts.append("")
        parts.append("USER:")
        parts.append(user_text)
        parts.append("")
        parts.append(
            "Respond helpfully as the active agent. If this is a coding task, "
            "work in the workspace when paths are provided. "
            "When finishing multi-step work, include a brief HANDOFF REPORT if useful."
        )
        return "\n".join(parts)

    async def maybe_compact(
        self,
        session: ChatSession,
        *,
        force: bool = False,
        reason: str = "context_limit",
    ) -> bool:
        """If near context limit (or force), summarize and open a new segment."""
        provider = session.active_provider
        budget = self._budget_for(provider)
        msgs = session.messages_as_dicts()
        usage = budget.usage(msgs, summary=session.memory_summary)
        if not force and not usage["needs_compaction"]:
            return False

        seg = session.ensure_segment()
        if len(seg.messages) < 2 and not force:
            return False

        adapter = self.app.registry.get(provider) if provider else None
        summary: str
        if self.prefer_llm_summary and adapter is not None:
            ws = str(self.app.settings.workspace_root / "_sessions" / session.session_id)
            Path(ws).mkdir(parents=True, exist_ok=True)
            summary = await self.compactor.summarize_with_adapter(
                adapter, seg.messages, session.memory_summary, ws
            )
        else:
            summary = self.compactor.extractive_summary(seg.messages, session.memory_summary)

        # close segment
        seg.closed_at = utcnow().isoformat()
        seg.close_reason = reason
        session.memory_summary = summary

        # new segment carries summary only + optional last user message stub
        new_seg = SessionSegment(
            index=seg.index + 1,
            summary_from_previous=summary,
            messages=[],
        )
        # keep last user message for continuity if exists
        for m in reversed(seg.messages):
            if m.role == "user":
                new_seg.messages.append(
                    ChatMessage(
                        role="system",
                        content=f"(Carried last user intent) {m.content[:1500]}",
                        meta={"carried": True},
                    )
                )
                break
        session.segments.append(new_seg)
        session.append(
            ChatMessage(
                role="system",
                content=f"[AETHER] Session compacted ({reason}). "
                f"Tokens were ~{usage['used_tokens']}/{usage['context_window']}. "
                f"Continuing in segment {new_seg.index} with summary memory.",
                meta={"event": "compacted", "usage": usage},
            )
        )
        self.store.save(session)
        return True

    async def switch_provider(
        self,
        session: ChatSession,
        failed_provider: str,
        reason: str,
    ) -> Optional[str]:
        """Pick next available provider and record switch."""
        from aether.domain.enums import ProviderAvailability

        candidates = self._enabled_providers()
        exclude = {failed_provider}
        # also skip open circuits / rate limited
        next_id: Optional[str] = None
        for pid in candidates:
            if pid in exclude:
                continue
            if not self.app.circuit.allow(pid):
                continue
            avail = self.app.quota.availability(pid)
            if avail in (
                ProviderAvailability.RATE_LIMITED,
                ProviderAvailability.QUOTA_EXHAUSTED,
                ProviderAvailability.CIRCUIT_OPEN,
            ):
                continue
            adapter = self.app.registry.get(pid)
            if not adapter:
                continue
            health = await adapter.health_check()
            cfg = self.app.registry.get_config(pid)
            if not health.available and not (cfg and cfg.for_testing):
                continue
            next_id = pid
            break

        if not next_id:
            session.append(
                ChatMessage(
                    role="system",
                    content=f"[AETHER] No failover provider available after {failed_provider} ({reason}).",
                    meta={"event": "failover_failed"},
                )
            )
            self.store.save(session)
            return None

        prev = session.active_provider
        session.active_provider = next_id
        if next_id not in session.participating_providers:
            session.participating_providers.append(next_id)
        session.append(
            ChatMessage(
                role="system",
                content=f"[AETHER] Model switch: {prev} → {next_id} (reason: {reason}). "
                f"Continuing with session memory intact.",
                provider_id=next_id,
                meta={"event": "model_switch", "from": prev, "to": next_id, "reason": reason},
            )
        )
        self.store.save(session)
        return next_id

    async def chat(
        self,
        session: ChatSession,
        user_text: str,
        *,
        workspace: Optional[str] = None,
        max_failovers: int = 4,
    ) -> dict[str, Any]:
        """Send one user turn; handle compaction + failover. Returns result dict for UI."""
        from aether.domain.enums import FailureClass
        from aether.domain.models import ExecutionRequest

        session.append(ChatMessage(role="user", content=user_text))
        self.store.save(session)

        # compact if needed before call
        compacted = await self.maybe_compact(session)
        events: list[str] = []
        if compacted:
            events.append("session_compacted")

        attempts = 0
        last_error = ""
        while attempts <= max_failovers:
            provider = session.active_provider
            if not provider:
                providers = self._enabled_providers()
                if not providers:
                    return {
                        "ok": False,
                        "error": "No enabled models. Run /setup",
                        "session": session,
                        "events": events,
                    }
                session.active_provider = providers[0]
                provider = session.active_provider

            # re-check budget for this provider (different windows)
            if await self.maybe_compact(session):
                events.append("session_compacted_pre_provider")

            adapter = self.app.registry.get(provider)
            if adapter is None:
                switched = await self.switch_provider(session, provider, "adapter_missing")
                events.append(f"switch:{provider}->missing")
                attempts += 1
                if not switched:
                    break
                continue

            # quota pre-check
            from aether.domain.enums import ProviderAvailability

            avail = self.app.quota.availability(provider)
            if avail != ProviderAvailability.AVAILABLE:
                switched = await self.switch_provider(session, provider, avail.value)
                events.append(f"switch:{provider}->{avail.value}")
                attempts += 1
                if not switched:
                    break
                continue

            prompt = self._build_prompt(session, user_text)
            ws = workspace or str(
                self.app.settings.workspace_root / "_sessions" / session.session_id
            )
            Path(ws).mkdir(parents=True, exist_ok=True)

            req = ExecutionRequest(
                task_id=f"CHAT-{session.session_id}",
                provider_id=provider,
                prompt=prompt,
                workspace_path=ws,
                timeout_seconds=self.app.settings.orchestrator.agent_timeout_seconds,
                context_package={
                    "session_id": session.session_id,
                    "segment": session.ensure_segment().index,
                    "summary": session.memory_summary[:2000],
                },
            )

            try:
                result = await adapter.execute(req)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                self.app.circuit.record_failure(provider)
                self.app.quota.record_failure(provider)
                switched = await self.switch_provider(session, provider, f"exception:{exc}")
                events.append(f"failover_exception:{provider}")
                attempts += 1
                if not switched:
                    break
                continue

            tokens = estimate_tokens(result.output or "") + estimate_tokens(prompt) // 8
            self.app.quota.record_request(provider, tokens=tokens)

            if not result.success:
                last_error = result.error or "failed"
                fc = result.failure_class or FailureClass.UNKNOWN
                self.app.circuit.record_failure(provider)
                self.app.quota.record_failure(provider)
                if fc == FailureClass.RATE_LIMIT:
                    self.app.quota.set_cooldown(provider, 60)
                if fc == FailureClass.QUOTA:
                    self.app.quota.mark_quota_exhausted(provider)
                # context overflow → compact then retry same or switch
                if fc == FailureClass.CONTEXT:
                    await self.maybe_compact(session, force=True, reason="provider_context_overflow")
                    events.append("forced_compact_context")
                    attempts += 1
                    continue
                switched = await self.switch_provider(session, provider, fc.value)
                events.append(f"failover:{provider}:{fc.value}")
                attempts += 1
                if not switched:
                    break
                continue

            # success
            self.app.circuit.record_success(provider)
            session.append(
                ChatMessage(
                    role="assistant",
                    content=result.output,
                    provider_id=provider,
                    model=getattr(self.app.registry.get_config(provider), "model", None),
                    meta={"latency_ms": result.latency_ms, "execution_id": result.execution_id},
                )
            )
            # post-success compact if now over limit
            if await self.maybe_compact(session):
                events.append("session_compacted_after_reply")
            self.store.save(session)
            budget = self._budget_for(provider)
            usage = budget.usage(session.messages_as_dicts(), summary=session.memory_summary)
            return {
                "ok": True,
                "reply": result.output,
                "provider_id": provider,
                "session": session,
                "events": events,
                "usage": usage,
                "latency_ms": result.latency_ms,
            }

        self.store.save(session)
        return {
            "ok": False,
            "error": last_error or "All providers failed",
            "session": session,
            "events": events,
        }

    async def chat_stream(
        self,
        session: ChatSession,
        user_text: str,
        *,
        workspace: Optional[str] = None,
        max_failovers: int = 4,
    ):
        """
        Async generator of stream events for live UI/terminal.

        Yields dicts:
          {type: status|token|event|done|error, ...}
        """
        from aether.domain.enums import FailureClass, ProviderAvailability
        from aether.domain.models import ExecutionRequest

        session.append(ChatMessage(role="user", content=user_text))
        self.store.save(session)

        events: list[str] = []
        if await self.maybe_compact(session):
            events.append("session_compacted")
            yield {"type": "event", "event": "session_compacted", "session_id": session.session_id}

        attempts = 0
        last_error = ""
        while attempts <= max_failovers:
            provider = session.active_provider
            if not provider:
                providers = self._enabled_providers()
                if not providers:
                    yield {"type": "error", "error": "No enabled models. Run /setup"}
                    return
                session.active_provider = providers[0]
                provider = session.active_provider

            if await self.maybe_compact(session):
                events.append("session_compacted_pre_provider")
                yield {"type": "event", "event": "session_compacted_pre_provider"}

            adapter = self.app.registry.get(provider)
            if adapter is None:
                switched = await self.switch_provider(session, provider, "adapter_missing")
                yield {"type": "event", "event": f"switch:{provider}->missing", "provider": switched}
                attempts += 1
                if not switched:
                    break
                continue

            avail = self.app.quota.availability(provider)
            if avail != ProviderAvailability.AVAILABLE:
                switched = await self.switch_provider(session, provider, avail.value)
                yield {
                    "type": "event",
                    "event": f"switch:{provider}->{avail.value}",
                    "provider": switched,
                }
                attempts += 1
                if not switched:
                    break
                continue

            yield {"type": "status", "provider": provider, "message": f"streaming via {provider}"}

            prompt = self._build_prompt(session, user_text)
            ws = workspace or str(
                self.app.settings.workspace_root / "_sessions" / session.session_id
            )
            Path(ws).mkdir(parents=True, exist_ok=True)
            req = ExecutionRequest(
                task_id=f"CHAT-{session.session_id}",
                provider_id=provider,
                prompt=prompt,
                workspace_path=ws,
                timeout_seconds=self.app.settings.orchestrator.agent_timeout_seconds,
                stream=True,
                context_package={
                    "session_id": session.session_id,
                    "segment": session.ensure_segment().index,
                    "summary": session.memory_summary[:2000],
                },
            )

            chunks: list[str] = []
            stream_error: Optional[str] = None
            try:
                async for chunk in adapter.stream(req):
                    if chunk.text:
                        chunks.append(chunk.text)
                        yield {
                            "type": "token",
                            "text": chunk.text,
                            "provider": provider,
                            "execution_id": req.execution_id,
                        }
                    if chunk.done:
                        if chunk.metadata.get("error"):
                            stream_error = str(chunk.metadata.get("error"))
                        break
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                self.app.circuit.record_failure(provider)
                self.app.quota.record_failure(provider)
                switched = await self.switch_provider(session, provider, f"exception:{exc}")
                yield {"type": "event", "event": f"failover_exception:{provider}", "provider": switched}
                attempts += 1
                if not switched:
                    break
                continue

            full = "".join(chunks)
            if stream_error or not full.strip():
                # fallback to non-stream execute once
                try:
                    result = await adapter.execute(req)
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc)
                    switched = await self.switch_provider(session, provider, f"exception:{exc}")
                    yield {"type": "event", "event": f"failover_exception:{provider}"}
                    attempts += 1
                    if not switched:
                        break
                    continue
                if not result.success:
                    last_error = result.error or "failed"
                    fc = result.failure_class or FailureClass.UNKNOWN
                    self.app.circuit.record_failure(provider)
                    self.app.quota.record_failure(provider)
                    if fc == FailureClass.RATE_LIMIT:
                        self.app.quota.set_cooldown(provider, 60)
                    if fc == FailureClass.QUOTA:
                        self.app.quota.mark_quota_exhausted(provider)
                    if fc == FailureClass.CONTEXT:
                        await self.maybe_compact(session, force=True, reason="provider_context_overflow")
                        yield {"type": "event", "event": "forced_compact_context"}
                        attempts += 1
                        continue
                    switched = await self.switch_provider(session, provider, fc.value)
                    yield {"type": "event", "event": f"failover:{provider}:{fc.value}", "provider": switched}
                    attempts += 1
                    if not switched:
                        break
                    continue
                full = result.output
                yield {"type": "token", "text": full, "provider": provider}

            self.app.circuit.record_success(provider)
            tokens = estimate_tokens(full) + estimate_tokens(prompt) // 8
            self.app.quota.record_request(provider, tokens=tokens)
            session.append(
                ChatMessage(
                    role="assistant",
                    content=full,
                    provider_id=provider,
                    model=getattr(self.app.registry.get_config(provider), "model", None),
                    meta={"streamed": True},
                )
            )
            if await self.maybe_compact(session):
                events.append("session_compacted_after_reply")
                yield {"type": "event", "event": "session_compacted_after_reply"}
            self.store.save(session)
            budget = self._budget_for(provider)
            usage = budget.usage(session.messages_as_dicts(), summary=session.memory_summary)
            yield {
                "type": "done",
                "ok": True,
                "reply": full,
                "provider_id": provider,
                "session_id": session.session_id,
                "segment": session.ensure_segment().index,
                "usage": usage,
                "events": events,
            }
            return

        self.store.save(session)
        yield {
            "type": "done",
            "ok": False,
            "error": last_error or "All providers failed",
            "session_id": session.session_id,
            "events": events,
        }
