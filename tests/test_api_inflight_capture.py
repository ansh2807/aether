"""API providers must not lose in-flight output when the process dies.

The CLI adapter has mirrored agent output to disk since WO-02, so a kill at
minute nine of a ten-minute run leaves nine minutes of work salvageable. The
API adapter did not: it issued one buffered POST and held the whole response in
memory until it completed. `docs/DURABILITY.md` recorded that honestly as a
partial guarantee.

It became the larger half of the gap once the provider catalogue filled up with
hosted APIs — OpenAI, Anthropic, DeepSeek, OpenRouter and the rest all took the
buffered path. These tests pin the streaming behaviour that closes it, and the
fallback that keeps servers without SSE working.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from aether.adapters.api_adapter import ApiAgentAdapter
from aether.domain.enums import AdapterType, FailureClass
from aether.domain.models import ExecutionRequest, ProviderConfig


def _config() -> ProviderConfig:
    return ProviderConfig(
        provider_id="testapi",
        display_name="Test API",
        adapter=AdapterType.API,
        enabled=True,
        base_url="https://api.example.com/v1",
        model="test-model",
        api_key_env="TEST_API_KEY",
    )


def _request(tmp_path: Path, subtask_id: str = "SUB-1") -> ExecutionRequest:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ExecutionRequest(
        task_id="TASK-1",
        subtask_id=subtask_id,
        provider_id="testapi",
        prompt="write something long",
        workspace_path=str(ws),
        timeout_seconds=30,
    )


def _sse(*chunks: str) -> bytes:
    lines = []
    for c in chunks:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"content": c}}]}))
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


def _adapter(handler, monkeypatch) -> ApiAgentAdapter:
    monkeypatch.setenv("TEST_API_KEY", "sk-test")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ApiAgentAdapter(_config(), client=client)


def _log_path(request: ExecutionRequest) -> Path:
    from aether.execution_log import ExecutionLog

    return ExecutionLog(request.workspace_path, request.subtask_id).path


# --- the guarantee -------------------------------------------------------


@pytest.mark.asyncio
async def test_output_reaches_disk_before_the_response_completes(tmp_path, monkeypatch):
    """The whole point: killing the process mid-generation must leave something.

    The assertion is made *during* the stream, not after — checking only at the
    end would pass against the old buffered adapter too, which also ends with
    the text available.
    """
    request = _request(tmp_path)
    seen_midway: list[str] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        assert json.loads(req.content)["stream"] is True, "did not ask for a stream"

        async def body():
            # An async generator, so httpx hands chunks to the adapter one at a
            # time. Passing bytes instead would materialise the whole body up
            # front and this test would prove nothing about *when* the write
            # happens — only that it eventually happens, which the old buffered
            # adapter also satisfied.
            yield _sse("part one. ")
            path = _log_path(request)
            seen_midway.append(path.read_text(encoding="utf-8") if path.exists() else "")
            yield _sse("part two.")

        return httpx.Response(200, content=body())

    adapter = _adapter(handler, monkeypatch)
    result = await adapter.execute(request)

    assert result.success, result.error
    assert "part one" in result.output and "part two" in result.output
    assert result.raw.get("streamed") is True

    # The assertion that matters: the first chunk was already durable while the
    # second was still in flight. That is what a SIGKILL at that instant keeps.
    assert seen_midway, "the stream was never observed mid-flight"
    assert "part one" in seen_midway[0], (
        f"nothing was on disk while the response was still arriving "
        f"(saw {seen_midway[0]!r}) — output is still buffered in memory"
    )
    assert "part two" not in seen_midway[0], "the test did not actually sample mid-stream"

    on_disk = _log_path(request).read_text(encoding="utf-8")
    assert "part one" in on_disk and "part two" in on_disk
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_partial_output_survives_an_abandoned_stream(tmp_path, monkeypatch):
    """A connection that dies mid-generation is the real failure being modelled."""
    request = _request(tmp_path)

    async def handler(req: httpx.Request) -> httpx.Response:
        # well-formed prefix, then the connection drops
        return httpx.Response(200, content=b"data: " + json.dumps(
            {"choices": [{"delta": {"content": "salvage me"}}]}).encode() + b"\n\n")

    adapter = _adapter(handler, monkeypatch)
    await adapter.execute(request)

    on_disk = _log_path(request).read_text(encoding="utf-8")
    assert "salvage me" in on_disk, "the partial generation was not persisted"
    await adapter.shutdown()


# --- not breaking anything ----------------------------------------------


@pytest.mark.asyncio
async def test_falls_back_when_the_server_cannot_stream(tmp_path, monkeypatch):
    """Not every OpenAI-compatible server implements SSE. Rejecting the stream
    must cost a fallback, not the request."""
    calls: list[bool] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        streaming = json.loads(req.content).get("stream", False)
        calls.append(streaming)
        if streaming:
            return httpx.Response(400, json={"error": "stream not supported"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "buffered ok"}}]})

    adapter = _adapter(handler, monkeypatch)
    result = await adapter.execute(_request(tmp_path))

    assert result.success, result.error
    assert result.output == "buffered ok"
    assert calls == [True, False], f"expected a stream attempt then a fallback, got {calls}"
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_an_empty_stream_falls_back_rather_than_returning_nothing(tmp_path, monkeypatch):
    async def handler(req: httpx.Request) -> httpx.Response:
        if json.loads(req.content).get("stream"):
            return httpx.Response(200, content=b"data: [DONE]\n\n")
        return httpx.Response(200, json={"choices": [{"message": {"content": "recovered"}}]})

    adapter = _adapter(handler, monkeypatch)
    result = await adapter.execute(_request(tmp_path))
    assert result.output == "recovered"
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_auth_failure_is_not_retried_as_a_fallback(tmp_path, monkeypatch):
    """A 401 is the same answer buffered or streamed. Sending it twice would
    burn a second request on a credential that cannot work — and WO-23 now
    treats AUTH as terminal, so the classification has to be right."""
    calls: list[bool] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        calls.append(json.loads(req.content).get("stream", False))
        return httpx.Response(401, json={"error": "bad key"})

    adapter = _adapter(handler, monkeypatch)
    result = await adapter.execute(_request(tmp_path))

    assert not result.success
    assert result.failure_class == FailureClass.AUTH
    assert calls == [True], f"a 401 was re-sent on the buffered path: {calls}"
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_rate_limit_is_classified_the_same_on_both_paths(tmp_path, monkeypatch):
    async def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down"})

    adapter = _adapter(handler, monkeypatch)
    result = await adapter.execute(_request(tmp_path))
    assert result.failure_class == FailureClass.RATE_LIMIT
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_no_workspace_means_no_streaming_and_no_crash(tmp_path, monkeypatch):
    """Health checks and ad-hoc calls have nowhere to mirror to. The adapter
    must simply use the buffered path rather than failing."""
    async def handler(req: httpx.Request) -> httpx.Response:
        assert not json.loads(req.content).get("stream"), "streamed with nowhere to log"
        return httpx.Response(200, json={"choices": [{"message": {"content": "fine"}}]})

    monkeypatch.setenv("TEST_API_KEY", "sk-test")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ApiAgentAdapter(_config(), client=client)

    request = ExecutionRequest(
        task_id="TASK-1", subtask_id=None, provider_id="testapi",
        prompt="hi", workspace_path="", timeout_seconds=30,
    )
    result = await adapter.execute(request)
    assert result.success and result.output == "fine"
    await adapter.shutdown()
