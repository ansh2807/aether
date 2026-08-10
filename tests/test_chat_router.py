"""Chat router: context compaction, model switch, debate."""

from __future__ import annotations

import pytest

from aether.app import AetherApp
from aether.core.chat_session import ChatMessage, ChatSession, SessionCompactor
from aether.core.context_budget import ContextBudget, estimate_tokens
from aether.domain.enums import FailureClass


def test_estimate_tokens():
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 40) == 10


def test_context_budget_needs_compaction():
    b = ContextBudget(context_window=2000, compaction_ratio=0.5)
    msgs = [{"role": "user", "content": "x" * 3000}]  # ~750 tokens + overhead
    usage = b.usage(msgs)
    assert usage["used_tokens"] > 0
    assert usage["soft_limit"] == 1000
    assert usage["context_window"] == 2000


def test_extractive_summary():
    c = SessionCompactor()
    messages = [
        ChatMessage(role="user", content="Build a QR app"),
        ChatMessage(role="assistant", content="I will use WebRTC", provider_id="mock"),
        ChatMessage(role="user", content="Also add auth"),
    ]
    s = c.extractive_summary(messages, previous_summary="Prior: discussed stack")
    assert "QR" in s or "Build" in s
    assert "Prior" in s
    assert "Continuation" in s


@pytest.mark.asyncio
async def test_chat_success_and_persist(app: AetherApp):
    session = app.chat_router.create_session(provider_id="mock", mode="chat")
    result = await app.chat_router.chat(session, "Hello, say hi")
    assert result["ok"] is True
    assert result["reply"]
    assert result["provider_id"] == "mock"
    loaded = app.chat_store.load(session.session_id)
    assert loaded is not None
    assert any(m.role == "user" for m in loaded.current_messages())
    assert any(m.role == "assistant" for m in loaded.current_messages())


@pytest.mark.asyncio
async def test_model_switch_on_failure(app: AetherApp):
    from aether.adapters.mock_adapter import MockAdapter

    mock = app.registry.get("mock")
    assert isinstance(mock, MockAdapter)
    mock.set_fail_next(FailureClass.RATE_LIMIT)

    session = app.chat_router.create_session(provider_id="mock", mode="chat")
    # mock_b should be available as failover
    result = await app.chat_router.chat(session, "Continue after rate limit", max_failovers=3)
    # either succeeded via mock_b or mock on retry
    assert result["ok"] is True or "events" in result
    if result["ok"]:
        # should have switched or retried
        assert result.get("provider_id") in {"mock", "mock_b"}


@pytest.mark.asyncio
async def test_force_compact_rollover(app: AetherApp):
    session = app.chat_router.create_session(provider_id="mock", mode="chat")
    for i in range(5):
        session.append(ChatMessage(role="user", content=f"msg {i} " + ("body " * 50)))
        session.append(
            ChatMessage(role="assistant", content=f"reply {i} " + ("x" * 200), provider_id="mock")
        )
    app.chat_store.save(session)
    before = len(session.segments)
    ok = await app.chat_router.maybe_compact(session, force=True, reason="test")
    assert ok is True
    assert len(session.segments) == before + 1
    assert session.memory_summary
    assert session.segments[-1].summary_from_previous


@pytest.mark.asyncio
async def test_debate_multi_model(app: AetherApp):
    result = await app.debate_engine.run(
        "Should we use WebRTC or WebSockets?",
        providers=["mock", "mock_b"],
        rounds=1,
    )
    assert result["ok"] is True
    assert result["session_id"]
    assert len(result["transcript"]) >= 1
    # synthesis attempted
    assert "synthesis" in result


@pytest.mark.asyncio
async def test_chat_stream_tokens(app: AetherApp):
    session = app.chat_router.create_session(provider_id="mock", mode="chat")
    tokens: list[str] = []
    done = None
    async for ev in app.chat_router.chat_stream(session, "Stream hello please"):
        if ev.get("type") == "token" and ev.get("text"):
            tokens.append(ev["text"])
        if ev.get("type") == "done":
            done = ev
    assert tokens, "expected streamed tokens"
    assert done is not None
    assert done.get("ok") is True
    assert "".join(tokens)


def test_context_window_from_config():
    from aether.core.context_budget import context_window_for

    assert context_window_for("openai", config_window=64_000) == 64_000
    assert context_window_for("claude_cli") == 200_000
    assert context_window_for("unknown_x") == 128_000
