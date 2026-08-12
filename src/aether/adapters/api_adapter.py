"""OpenAI-compatible API adapter.

Works with OpenAI, xAI (Grok), local OpenAI-compatible servers, and similar.
Requires API key from environment variable named in config (never from task memory).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, AsyncIterator, Optional

import httpx

from aether.adapters.base import BaseAdapter
from aether.domain.enums import FailureClass, ProviderAvailability
from aether.execution_log import ExecutionLog
from aether.domain.models import (
    AuthStatus,
    ExecutionRequest,
    ExecutionResult,
    HealthStatus,
    ProviderConfig,
    StreamChunk,
    utcnow,
)
from aether.observability.logging import get_logger

log = get_logger("aether.adapters.api")


class ApiAgentAdapter(BaseAdapter):
    def __init__(
        self,
        config: ProviderConfig,
        client: Optional[httpx.AsyncClient] = None,
        network_retries: Optional[int] = None,
    ) -> None:
        super().__init__(config, network_retries=network_retries)
        self._client = client
        self._owns_client = client is None

    def _api_key(self) -> Optional[str]:
        env_name = self.config.api_key_env or "OPENAI_API_KEY"
        return os.environ.get(env_name)

    async def initialize(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
        self._initialized = True

    async def shutdown(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
        await super().shutdown()

    async def authenticate(self) -> AuthStatus:
        key = self._api_key()
        if not key:
            return AuthStatus(
                authenticated=False,
                method="api_key_env",
                message=f"Missing env {self.config.api_key_env or 'OPENAI_API_KEY'}",
            )
        return AuthStatus(authenticated=True, method="api_key_env", message="API key present in environment")

    async def health_check(self) -> HealthStatus:
        if not self.config.enabled:
            return HealthStatus(
                provider_id=self.provider_id,
                available=False,
                availability=ProviderAvailability.DISABLED,
                message="disabled",
                checked_at=utcnow(),
            )
        auth = await self.authenticate()
        if not auth.authenticated:
            return HealthStatus(
                provider_id=self.provider_id,
                available=False,
                availability=ProviderAvailability.AUTH_REQUIRED,
                message=auth.message,
                checked_at=utcnow(),
            )
        if not self.config.base_url:
            return HealthStatus(
                provider_id=self.provider_id,
                available=False,
                availability=ProviderAvailability.ADAPTER_REQUIRED,
                message="base_url not configured",
                checked_at=utcnow(),
            )
        # Light check: models endpoint if available
        assert self._client is not None or True
        await self.initialize()
        assert self._client is not None
        url = self.config.base_url.rstrip("/") + "/models"
        start = time.perf_counter()
        try:
            resp = await self._client.get(
                url,
                headers={"Authorization": f"Bearer {self._api_key()}"},
            )
            latency = (time.perf_counter() - start) * 1000
            if resp.status_code in (401, 403):
                return HealthStatus(
                    provider_id=self.provider_id,
                    available=False,
                    availability=ProviderAvailability.AUTH_REQUIRED,
                    latency_ms=latency,
                    message=f"auth failed: HTTP {resp.status_code}",
                    checked_at=utcnow(),
                )
            # 404 on /models is ok for some providers — still consider configured
            return HealthStatus(
                provider_id=self.provider_id,
                available=True,
                availability=ProviderAvailability.AVAILABLE,
                latency_ms=latency,
                message=f"reachable HTTP {resp.status_code}",
                checked_at=utcnow(),
            )
        except Exception as exc:  # noqa: BLE001
            return HealthStatus(
                provider_id=self.provider_id,
                available=False,
                availability=ProviderAvailability.UNAVAILABLE,
                message=str(exc),
                checked_at=utcnow(),
            )

    async def _generate_image(self, request: ExecutionRequest, start: float) -> ExecutionResult:
        """Produce an actual image via the OpenAI-compatible images endpoint.

        This is what makes image modality real rather than declared. Without
        it, routing a "generate a logo" subtask to a provider marked
        image-capable would still return a paragraph describing a logo — the
        gate in the router would be enforcing a promise nothing kept.

        `/images/generations` is the one media endpoint with a common shape
        across OpenAI-compatible servers. Video and audio have no equivalent,
        which is why they stay unserved and fail loudly instead.
        """
        await self.initialize()
        assert self._client is not None
        key = self._api_key()
        url = self.config.base_url.rstrip("/") + "/images/generations"
        model = (self.config.extra or {}).get("image_model") or "gpt-image-1"

        try:
            resp = await self._client.post(
                url,
                json={"model": model, "prompt": request.prompt[:4000], "n": 1},
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                timeout=request.timeout_seconds,
            )
        except httpx.TimeoutException:
            self._record_failure()
            return ExecutionResult(
                execution_id=request.execution_id, provider_id=self.provider_id,
                success=False, error="image generation timed out",
                failure_class=FailureClass.TIMEOUT,
                latency_ms=(time.perf_counter() - start) * 1000,
            )
        except httpx.HTTPError as exc:
            self._record_failure()
            return ExecutionResult(
                execution_id=request.execution_id, provider_id=self.provider_id,
                success=False, error=str(exc), failure_class=FailureClass.NETWORK,
                latency_ms=(time.perf_counter() - start) * 1000,
            )

        if resp.status_code >= 400:
            return self._error_result(request, resp.status_code, resp.text, start)

        try:
            item = resp.json()["data"][0]
        except (ValueError, KeyError, IndexError) as exc:
            self._record_failure()
            return ExecutionResult(
                execution_id=request.execution_id, provider_id=self.provider_id,
                success=False, error=f"unreadable image response: {exc}",
                failure_class=FailureClass.INVALID_RESPONSE,
                latency_ms=(time.perf_counter() - start) * 1000,
            )

        # The artifact has to land on disk. An image returned only in the
        # response body would vanish with the process, and verification checks
        # artifacts rather than claims.
        path = await self._save_image(item, request)
        if path is None:
            self._record_failure()
            return ExecutionResult(
                execution_id=request.execution_id, provider_id=self.provider_id,
                success=False, error="image returned but could not be saved",
                failure_class=FailureClass.INVALID_RESPONSE,
                latency_ms=(time.perf_counter() - start) * 1000,
            )

        self._record_request(tokens=0)
        return ExecutionResult(
            execution_id=request.execution_id, provider_id=self.provider_id,
            success=True,
            # The handoff format the orchestrator parses, so a generated image
            # completes its subtask like any other piece of work.
            output=(
                "STATUS: COMPLETE\n\nCOMPLETED:\n"
                f"- generated image at {path.name}\n"
            ),
            artifacts=[str(path)],
            latency_ms=(time.perf_counter() - start) * 1000,
            raw={"model": model, "modality": "image"},
        )

    async def _save_image(self, item: dict, request: ExecutionRequest):
        """Write a returned image to the workspace, base64 or URL."""
        import base64
        from pathlib import Path

        workspace = Path(request.workspace_path or ".")
        try:
            workspace.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        out = workspace / f"generated-{request.execution_id}.png"

        if item.get("b64_json"):
            try:
                out.write_bytes(base64.b64decode(item["b64_json"]))
                return out
            except (ValueError, OSError):
                return None

        if item.get("url") and self._client is not None:
            try:
                got = await self._client.get(item["url"], timeout=60)
                if got.status_code == 200:
                    out.write_bytes(got.content)
                    return out
            except (httpx.HTTPError, OSError):
                return None
        return None

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Retry NETWORK/TIMEOUT failures with backoff before returning.

        The orchestrator only sees the final result of this call, so a
        retry-exhausted attempt still costs exactly one circuit-breaker
        failure, not one per try.
        """
        # A subtask the router gated as needing an image goes to the images
        # endpoint. Sending it to /chat/completions would return prose about
        # an image, which is the exact failure the gate exists to prevent.
        if "image" in (request.metadata or {}).get("modalities", []):
            start = time.perf_counter()
            return await self._generate_image(request, start)
        return await self._execute_with_retry(lambda: self._execute_once(request))

    def _error_result(
        self, request: ExecutionRequest, status: int, text: str, start: float
    ) -> ExecutionResult:
        """Classify an HTTP error the same way on both the streaming and the
        buffered path, so a 401 does not mean one thing over SSE and another
        over a plain POST."""
        if status == 429:
            failure = FailureClass.RATE_LIMIT
        elif status in (401, 403):
            failure = FailureClass.AUTH
        else:
            failure = self.classify_error(text)
        self._record_failure()
        return ExecutionResult(
            execution_id=request.execution_id,
            provider_id=self.provider_id,
            success=False,
            error=text[:500] if status in (401, 403, 429) else f"HTTP {status}: {text[:500]}",
            failure_class=failure,
            latency_ms=(time.perf_counter() - start) * 1000,
        )

    async def _execute_streaming(
        self,
        request: ExecutionRequest,
        url: str,
        body: dict,
        headers: dict,
        exec_log: "ExecutionLog",
        start: float,
    ) -> Optional[ExecutionResult]:
        """Run the request over SSE, mirroring output to disk as it arrives.

        This is the API-side half of the durability bet. A buffered POST holds
        the entire response in memory until it completes, so killing the process
        at minute nine of a ten-minute generation loses all of it — the CLI
        adapter has streamed to disk since WO-02, and API providers did not,
        which became the larger gap once the catalogue filled with them.

        Returns `None` when streaming is not usable (the endpoint rejects it, or
        sends nothing parseable), so the caller falls back to the buffered path
        rather than failing a request that would otherwise have worked. Not
        every OpenAI-compatible server implements SSE.
        """
        stream_body = {**body, "stream": True}
        pieces: list[str] = []
        try:
            async with self._client.stream(
                "POST", url, json=stream_body, headers=headers,
                timeout=request.timeout_seconds,
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    text = resp.text
                    # 400/404/422 most likely mean "this server does not stream";
                    # fall back rather than reporting a provider error.
                    if resp.status_code in (400, 404, 422):
                        return None
                    return self._error_result(request, resp.status_code, text, start)

                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(payload)
                        delta = chunk["choices"][0].get("delta") or {}
                        piece = delta.get("content") or ""
                    except (ValueError, KeyError, IndexError):
                        continue
                    if piece:
                        pieces.append(piece)
                        # the whole point: on disk before the process can die
                        exec_log.append(piece)
        except httpx.TimeoutException:
            # Not a fallback case. Partial output is already on disk, and the
            # buffered path would only wait all over again.
            self._record_failure()
            return ExecutionResult(
                execution_id=request.execution_id,
                provider_id=self.provider_id,
                success=False,
                error="API timeout",
                failure_class=FailureClass.TIMEOUT,
                latency_ms=(time.perf_counter() - start) * 1000,
            )
        except httpx.HTTPError:
            return None  # let the buffered path classify it

        content = "".join(pieces)
        if not content:
            return None  # nothing arrived; the buffered path may still work

        handoff = self.parse_handoff(
            request.task_id, self.provider_id, content, requested=request.prompt[:300]
        )
        self._record_request(tokens=len(content) // 4)
        return ExecutionResult(
            execution_id=request.execution_id,
            provider_id=self.provider_id,
            success=True,
            output=content,
            handoff=handoff,
            latency_ms=(time.perf_counter() - start) * 1000,
            raw={"model": self.config.model, "streamed": True},
        )

    async def _execute_once(self, request: ExecutionRequest) -> ExecutionResult:
        start = time.perf_counter()
        await self.initialize()
        assert self._client is not None
        key = self._api_key()
        if not key:
            self._record_failure()
            return ExecutionResult(
                execution_id=request.execution_id,
                provider_id=self.provider_id,
                success=False,
                error=f"Missing API key env {self.config.api_key_env}",
                failure_class=FailureClass.AUTH,
                latency_ms=(time.perf_counter() - start) * 1000,
            )

        url = self.config.base_url.rstrip("/") + "/chat/completions"
        body: dict[str, Any] = {
            "model": self.config.model or "gpt-4o",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an agent working under the AETHER orchestrator. "
                        "Follow the assignment. When done, output a HANDOFF REPORT with "
                        "STATUS, COMPLETED, REMAINING, FILES, TESTS, KNOWN_BUGS, "
                        "ASSUMPTIONS, RECOMMENDED_NEXT_ACTION."
                    ),
                },
                {"role": "user", "content": request.prompt},
            ],
            "temperature": float((self.config.extra or {}).get("temperature", 0.2)),
        }

        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        # Stream when there is somewhere to mirror to, so a kill mid-generation
        # leaves salvageable output on disk. Falls through to the buffered POST
        # below if the endpoint cannot stream.
        exec_log = ExecutionLog.for_request(request.workspace_path, request.subtask_id)
        if exec_log is not None:
            self._active[request.execution_id] = True
            try:
                streamed = await self._execute_streaming(
                    request, url, body, headers, exec_log, start
                )
            finally:
                self._active.pop(request.execution_id, None)
            if streamed is not None:
                return streamed

        self._active[request.execution_id] = True
        try:
            resp = await self._client.post(url, json=body, headers=headers, timeout=request.timeout_seconds)
        except httpx.TimeoutException:
            self._record_failure()
            self._active.pop(request.execution_id, None)
            return ExecutionResult(
                execution_id=request.execution_id,
                provider_id=self.provider_id,
                success=False,
                error="API timeout",
                failure_class=FailureClass.TIMEOUT,
                latency_ms=(time.perf_counter() - start) * 1000,
            )
        except httpx.HTTPError as exc:
            self._record_failure()
            self._active.pop(request.execution_id, None)
            return ExecutionResult(
                execution_id=request.execution_id,
                provider_id=self.provider_id,
                success=False,
                error=str(exc),
                failure_class=FailureClass.NETWORK,
                latency_ms=(time.perf_counter() - start) * 1000,
            )
        finally:
            self._active.pop(request.execution_id, None)

        text = resp.text
        if resp.status_code == 429:
            self._record_failure()
            return ExecutionResult(
                execution_id=request.execution_id,
                provider_id=self.provider_id,
                success=False,
                error=text[:500],
                failure_class=FailureClass.RATE_LIMIT,
                latency_ms=(time.perf_counter() - start) * 1000,
            )
        if resp.status_code in (401, 403):
            self._record_failure()
            return ExecutionResult(
                execution_id=request.execution_id,
                provider_id=self.provider_id,
                success=False,
                error=text[:500],
                failure_class=FailureClass.AUTH,
                latency_ms=(time.perf_counter() - start) * 1000,
            )
        if resp.status_code >= 400:
            self._record_failure()
            return ExecutionResult(
                execution_id=request.execution_id,
                provider_id=self.provider_id,
                success=False,
                error=f"HTTP {resp.status_code}: {text[:500]}",
                failure_class=self.classify_error(text),
                latency_ms=(time.perf_counter() - start) * 1000,
            )

        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            self._record_failure()
            return ExecutionResult(
                execution_id=request.execution_id,
                provider_id=self.provider_id,
                success=False,
                error=f"Invalid API response: {exc}",
                failure_class=FailureClass.INVALID_RESPONSE,
                latency_ms=(time.perf_counter() - start) * 1000,
                raw={"body": text[:2000]},
            )

        handoff = self.parse_handoff(
            request.task_id, self.provider_id, content, requested=request.prompt[:300]
        )
        self._record_request(tokens=len(content) // 4)
        return ExecutionResult(
            execution_id=request.execution_id,
            provider_id=self.provider_id,
            success=True,
            output=content,
            handoff=handoff,
            latency_ms=(time.perf_counter() - start) * 1000,
            raw={"model": self.config.model},
        )

    async def stream(self, request: ExecutionRequest) -> AsyncIterator[StreamChunk]:
        """Prefer OpenAI-compatible SSE stream; fall back to chunked execute."""
        await self.initialize()
        assert self._client is not None
        key = self._api_key()
        if not key or not self.config.base_url:
            result = await self.execute(request)
            if result.output:
                yield StreamChunk(execution_id=request.execution_id, text=result.output, done=False)
            yield StreamChunk(
                execution_id=request.execution_id,
                text="",
                done=True,
                metadata={"error": result.error or "", "success": result.success},
            )
            return

        url = self.config.base_url.rstrip("/") + "/chat/completions"
        body: dict[str, Any] = {
            "model": self.config.model or "gpt-4o",
            "stream": True,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an agent under AETHER. Follow the assignment. "
                        "For chat, answer helpfully and concisely."
                    ),
                },
                {"role": "user", "content": request.prompt},
            ],
            "temperature": float((self.config.extra or {}).get("temperature", 0.2)),
        }
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            async with self._client.stream(
                "POST",
                url,
                json=body,
                headers=headers,
                timeout=request.timeout_seconds,
            ) as resp:
                if resp.status_code >= 400:
                    err = (await resp.aread()).decode("utf-8", errors="replace")[:500]
                    yield StreamChunk(
                        execution_id=request.execution_id,
                        text="",
                        done=True,
                        metadata={"error": f"HTTP {resp.status_code}: {err}", "success": False},
                    )
                    return
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("data: "):
                        payload = line[6:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            import json as _json

                            data = _json.loads(payload)
                            delta = data["choices"][0].get("delta") or {}
                            piece = delta.get("content") or ""
                            if piece:
                                yield StreamChunk(
                                    execution_id=request.execution_id,
                                    text=piece,
                                    done=False,
                                )
                        except Exception:  # noqa: BLE001
                            continue
                yield StreamChunk(execution_id=request.execution_id, text="", done=True)
                return
        except Exception:
            # fallback non-stream
            result = await self.execute(request)
            step = max(20, len(result.output or "") // 20)
            text = result.output or ""
            for i in range(0, len(text), step or 1):
                yield StreamChunk(
                    execution_id=request.execution_id,
                    text=text[i : i + step],
                    done=False,
                )
            yield StreamChunk(
                execution_id=request.execution_id,
                text="",
                done=True,
                metadata={"success": result.success, "error": result.error or ""},
            )
