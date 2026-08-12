"""Media work must not be handed to a text model.

Every capability dimension used to be text-work, so "generate a 10-second
product video" scored as *coding* and went to whichever model writes the best
Python — which returned a confident paragraph describing a video. A plausible
wrong answer is the worst failure this project recognises, so modality gates
routing rather than weighting it.

Two claims are tested here and they pull in opposite directions:

  * a subtask that must produce media only goes to a provider that can, and
  * a provider only declares a modality AETHER can actually invoke.

Together they mean image work routes and runs, while video and audio fail
loudly with a reason — rather than quietly returning prose.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from aether.core.modality import (
    MEDIA_CAPABILITIES,
    SERVED_MODALITIES,
    describe_gap,
    detect_modalities,
    tag_modalities,
)
from aether.domain.enums import AdapterType, AgentRole, TaskMode
from aether.domain.models import ExecutionRequest, ProviderConfig, Subtask


# --- detection -----------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("Generate a product logo for the launch", {"image"}),
    ("Create a 10-second promo video", {"video"}),
    ("Record a voiceover for the intro", {"audio"}),
    ("Design a poster and produce a trailer", {"image", "video"}),
])
def test_media_work_is_detected(text, expected):
    assert detect_modalities(text) == expected


@pytest.mark.parametrize("text", [
    "Fix the video player buffering bug",
    "Add image upload to the profile endpoint",
    "Build an audio transcoding pipeline",
    "Write a component for the image gallery",
    "Refactor the thumbnail generation library",
])
def test_software_that_handles_media_is_not_media_work(text):
    """The expensive false positive: routing ordinary coding to an image model
    fails the subtask outright."""
    assert detect_modalities(text) == set(), f"{text!r} was mistaken for media work"


@pytest.mark.parametrize("text", [
    "Add a diagram to the README",
    "Document the video codec choices",
    "Review the marketing copy",
    "",
])
def test_ordinary_work_detects_nothing(text):
    assert detect_modalities(text) == set()


def test_detection_is_deterministic():
    """Same reason routing and staffing are: it must work when the planner's
    provider is down, and be explicable afterwards."""
    text = "Generate a hero image and create a launch video"
    assert len({frozenset(detect_modalities(text)) for _ in range(5)}) == 1


def test_tagging_adds_capabilities_without_dropping_existing_ones():
    subs = [
        Subtask(task_id="T", title="Generate the hero image",
                required_capabilities=["coding"], role=AgentRole.IMPLEMENTER),
        Subtask(task_id="T", title="Write the tests",
                required_capabilities=["testing"], role=AgentRole.TESTER),
    ]
    found = tag_modalities(subs)
    assert found == ["image"]
    assert set(subs[0].required_capabilities) == {"coding", "image"}
    assert subs[1].required_capabilities == ["testing"], "a text subtask was tagged"


def test_tagging_is_idempotent():
    sub = Subtask(task_id="T", title="Generate a logo", required_capabilities=["coding"])
    tag_modalities([sub])
    tag_modalities([sub])
    assert sub.required_capabilities.count("image") == 1


# --- what we admit we cannot do -----------------------------------------


def test_only_invocable_modalities_are_served():
    """The "no fake integrations" rule applied to modality. Gemini can make
    video; AETHER cannot ask it to, so video is not served."""
    assert SERVED_MODALITIES == {"image"}
    assert MEDIA_CAPABILITIES == {"image", "video", "audio"}


def test_the_gap_message_names_the_missing_piece_and_a_reason():
    gap = describe_gap({"video"})
    assert "video" in gap
    assert "no standard endpoint" in gap
    assert "description instead of the artifact" in gap, (
        "the message must say what would go wrong, not just that it failed"
    )
    assert describe_gap({"image"}) == "", "image is served; there is no gap"


def test_no_provider_claims_a_modality_aether_cannot_invoke():
    """A capability score is a promise the adapter has to keep."""
    from aether.config.providers import ALL_PROVIDERS

    for p in ALL_PROVIDERS:
        for m in MEDIA_CAPABILITIES - SERVED_MODALITIES:
            assert p.capabilities.get(m, 0) == 0, (
                f"{p.id} declares {m}, which no adapter can invoke — "
                f"routing would select it and then return text"
            )


# --- the gate ------------------------------------------------------------


async def _router(tmp_path: Path, providers: dict):
    from aether.app import AetherApp
    from aether.config.settings import AetherSettings, OrchestratorSettings

    s = AetherSettings(
        data_dir=tmp_path / "d", workspace_root=tmp_path / "w",
        database_url="sqlite+aiosqlite:///" + (tmp_path / "d" / "t.db").as_posix(),
        orchestrator=OrchestratorSettings(enable_git_checkpoints=False),
    )
    s.providers = providers
    s.ensure_dirs()
    app = AetherApp(s)
    await app.start()
    return app


def _mock(pid: str, **caps) -> ProviderConfig:
    base = {"coding": 9, "reasoning": 9, "architecture": 9, "testing": 9, "review": 9}
    return ProviderConfig(
        provider_id=pid, display_name=pid, adapter=AdapterType.MOCK,
        enabled=True, for_testing=True, capabilities={**base, **caps},
    )


@pytest.mark.asyncio
async def test_a_text_only_provider_never_gets_media_work(tmp_path: Path):
    """The whole point. Before this, the best coder won and returned prose."""
    app = await _router(tmp_path, {"texty": _mock("texty")})
    try:
        task = await app.orchestrator.submit("x", mode=TaskMode.SOLO, auto_start=False)
        sub = Subtask(task_id=task.task_id, title="Generate a logo",
                      required_capabilities=["coding", "image"],
                      role=AgentRole.IMPLEMENTER)
        decision = await app.router.select(task, subtask=sub)
        assert decision is None, (
            f"media work routed to {decision.provider_id}, which cannot produce an image"
        )
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_an_image_capable_provider_does_get_it(tmp_path: Path):
    """The gate must not be a blanket refusal."""
    app = await _router(tmp_path, {
        "texty": _mock("texty"),
        "arty": _mock("arty", image=8),
    })
    try:
        task = await app.orchestrator.submit("x", mode=TaskMode.SOLO, auto_start=False)
        sub = Subtask(task_id=task.task_id, title="Generate a logo",
                      required_capabilities=["coding", "image"],
                      role=AgentRole.IMPLEMENTER)
        decision = await app.router.select(task, subtask=sub)
        assert decision is not None and decision.provider_id == "arty"
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_text_subtasks_are_unaffected_by_the_gate(tmp_path: Path):
    app = await _router(tmp_path, {"texty": _mock("texty")})
    try:
        task = await app.orchestrator.submit("x", mode=TaskMode.SOLO, auto_start=False)
        sub = Subtask(task_id=task.task_id, title="Write the tests",
                      required_capabilities=["testing"], role=AgentRole.TESTER)
        decision = await app.router.select(task, subtask=sub)
        assert decision is not None and decision.provider_id == "texty"
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_the_refusal_explains_itself(tmp_path: Path):
    """"No available agents" reads as "try later". A modality nothing can serve
    is permanent and needs a different answer from the operator."""
    app = await _router(tmp_path, {"texty": _mock("texty")})
    try:
        task = await app.orchestrator.submit("x", mode=TaskMode.SOLO, auto_start=False)
        sub = Subtask(task_id=task.task_id, title="Create a promo video",
                      required_capabilities=["video"], role=AgentRole.IMPLEMENTER)
        assert await app.router.select(task, subtask=sub) is None
        assert "video" in app.router.last_failure_reason
        assert app.router.last_failure_reason != ""
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_the_mock_is_not_exempt_from_the_gate(tmp_path: Path):
    """Fabricating an image is the same lie as fabricating a success, and the
    mock is the one provider most likely to be enabled."""
    app = await _router(tmp_path, {"mock": _mock("mock")})
    try:
        task = await app.orchestrator.submit("x", mode=TaskMode.SOLO, auto_start=False)
        sub = Subtask(task_id=task.task_id, title="Generate a logo",
                      required_capabilities=["image"], role=AgentRole.IMPLEMENTER)
        assert await app.router.select(task, subtask=sub) is None
    finally:
        await app.stop()


# --- image generation actually produces a file ---------------------------


@pytest.mark.asyncio
async def test_an_image_subtask_writes_a_real_file(tmp_path: Path, monkeypatch):
    """Without this the gate would enforce a promise nothing keeps: the right
    provider selected, then asked for text anyway."""
    import base64

    from aether.adapters.api_adapter import ApiAgentAdapter

    png = base64.b64encode(b"\x89PNG\r\n\x1a\n fake pixels").decode()
    calls: list[str] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        assert req.url.path.endswith("/images/generations"), (
            f"an image subtask hit {req.url.path}, which returns prose"
        )
        assert json.loads(req.content)["prompt"]
        return httpx.Response(200, json={"data": [{"b64_json": png}]})

    monkeypatch.setenv("TEST_KEY", "sk-x")
    adapter = ApiAgentAdapter(
        ProviderConfig(provider_id="arty", display_name="arty", adapter=AdapterType.API,
                       enabled=True, base_url="https://example.com/v1",
                       model="m", api_key_env="TEST_KEY"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    result = await adapter.execute(ExecutionRequest(
        task_id="T", subtask_id="s", provider_id="arty",
        prompt="a minimal geometric logo", workspace_path=str(ws),
        timeout_seconds=30, metadata={"modalities": ["image"]},
    ))

    assert result.success, result.error
    assert result.artifacts, "no artifact recorded"
    written = Path(result.artifacts[0])
    assert written.exists() and written.read_bytes().startswith(b"\x89PNG"), (
        "the image was reported but never landed on disk"
    )
    assert "STATUS: COMPLETE" in result.output, "handoff format broken"
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_a_text_subtask_still_uses_chat_completions(tmp_path: Path, monkeypatch):
    """The image path must be entered only when the router asked for it."""
    async def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/chat/completions"), req.url.path
        return httpx.Response(200, json={"choices": [{"message": {"content": "text"}}]})

    from aether.adapters.api_adapter import ApiAgentAdapter

    monkeypatch.setenv("TEST_KEY", "sk-x")
    adapter = ApiAgentAdapter(
        ProviderConfig(provider_id="p", display_name="p", adapter=AdapterType.API,
                       enabled=True, base_url="https://example.com/v1",
                       model="m", api_key_env="TEST_KEY"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await adapter.execute(ExecutionRequest(
        task_id="T", provider_id="p", prompt="hi", workspace_path="", timeout_seconds=30,
    ))
    assert result.success
    await adapter.shutdown()
