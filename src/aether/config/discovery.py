"""Ask each provider what models it currently serves.

The catalogue in `providers.py` carries a `default_model` per provider, and
those strings age badly: `gpt-4o`, `grok-2-latest`, `gemini-2.0-flash` were
right when they were written and are not what anyone wants to run now. Worse,
they are wrong in the direction that hurts — a name that used to work now
returns a 404 from a config file that looks authoritative.

Nothing here hardcodes a replacement. A list of "latest" models baked into this
repository would be stale by the next release and stale in the same silent way.
Instead the provider is asked, over the `/v1/models` endpoint that almost every
OpenAI-compatible API already exposes — OpenAI, Groq, Together, Fireworks,
DeepSeek, Mistral, xAI, GLM, Cerebras, OpenRouter, and the local runtimes.

    aether models --refresh

Two things this deliberately does not do:

**It does not rewrite your config.** Discovery prints what is available and
leaves the choice to you. A tool that silently repointed a running deployment
at a model it found this morning would be a genuinely bad surprise.

**It does not guess when it cannot reach the endpoint.** A provider that does
not answer is reported as unreachable, not as having no models — the two mean
very different things to someone deciding what to run.
"""

from __future__ import annotations

import asyncio
import shutil
import os
from dataclasses import dataclass, field

import httpx

from aether.domain.models import ProviderConfig

# Long enough for a cold serverless endpoint, short enough that refreshing
# thirty providers does not feel broken.
TIMEOUT_SECONDS = 12.0

# A provider may serve hundreds (OpenRouter serves thousands). Nobody reads
# that in a terminal, and the newest are what the command is for.
MAX_SHOWN = 40


@dataclass
class Discovery:
    """What one provider answered when asked."""

    provider_id: str
    models: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def summary(self) -> str:
        if self.error:
            return self.error
        if not self.models:
            # Reachable and genuinely empty — a local runtime with nothing
            # pulled yet. Distinct from "could not ask", and says what to do.
            return "no models served (pull one first?)"
        return f"{len(self.models)} models"


def _key_for(cfg: ProviderConfig) -> str:
    return os.environ.get(cfg.api_key_env or "", "") if cfg.api_key_env else ""


def _models_url(base_url: str) -> str:
    base = (base_url or "").rstrip("/")
    if not base:
        return ""
    # Some catalogue entries already end in /v1, some do not, and Gemini's
    # compatibility shim ends in /openai. Appending blindly produces
    # /v1/v1/models against half of them.
    if base.endswith("/models"):
        return base
    if base.endswith("/v1") or base.endswith("/openai") or base.endswith("/v1beta"):
        return f"{base}/models"
    return f"{base}/v1/models"


def _parse(payload: object) -> list[str]:
    """Pull model ids out of a response, tolerantly.

    The OpenAI shape is `{"data": [{"id": ...}]}` and most providers match it,
    but not all: Ollama returns `{"models": [{"name": ...}]}`. Both are read
    rather than insisting on one, because failing to list a local runtime's
    models on a formatting technicality helps nobody.
    """
    if not isinstance(payload, dict):
        return []
    rows = payload.get("data")
    if not isinstance(rows, list):
        rows = payload.get("models")
    if not isinstance(rows, list):
        return []
    out: list[str] = []
    for row in rows:
        if isinstance(row, str):
            out.append(row)
        elif isinstance(row, dict):
            name = row.get("id") or row.get("name") or row.get("model")
            if isinstance(name, str) and name:
                out.append(name)
    return out


async def discover(
    cfg: ProviderConfig, client: httpx.AsyncClient | None = None
) -> Discovery:
    """Ask one provider. Never raises — an unreachable provider is a result."""
    url = _models_url(cfg.base_url)
    if not url:
        return Discovery(cfg.provider_id, error="no base_url (not an HTTP provider)")

    key = _key_for(cfg)
    if cfg.api_key_env and not key:
        # Not an error worth a stack trace: it is the normal state of a
        # provider you have not connected, and the fix is `aether connect`.
        return Discovery(cfg.provider_id, error=f"no key in ${cfg.api_key_env}")

    headers = {"Authorization": f"Bearer {key}"} if key else {}
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=5.0))
    try:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 401:
            return Discovery(cfg.provider_id, error="key rejected (401)")
        if resp.status_code == 404:
            return Discovery(cfg.provider_id, error="no /models endpoint (404)")
        if resp.status_code >= 400:
            return Discovery(cfg.provider_id, error=f"HTTP {resp.status_code}")
        return Discovery(cfg.provider_id, models=_parse(resp.json()))
    except httpx.TimeoutException:
        return Discovery(cfg.provider_id, error=f"timed out after {TIMEOUT_SECONDS:.0f}s")
    except (httpx.HTTPError, ValueError) as exc:
        return Discovery(cfg.provider_id, error=f"{type(exc).__name__}: {exc}"[:80])
    finally:
        if owns_client:
            await client.aclose()


async def discover_all(
    configs: list[ProviderConfig], client: httpx.AsyncClient | None = None
) -> list[Discovery]:
    """Ask every provider at once.

    Concurrently, because thirty providers at up to twelve seconds each is six
    minutes serially and nobody waits for that.
    """
    if not configs:
        return []
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_SECONDS, connect=5.0))
    try:
        return list(await asyncio.gather(*(discover(c, client) for c in configs)))
    finally:
        if owns_client:
            await client.aclose()


async def discover_cli(command: str, timeout: float = 10.0) -> Discovery:
    """Ask a CLI binary what its --model option accepts.

    CLI providers have no base_url, so `discover` skips them — and that is
    precisely where guessing hurts most, because a subscription CLI is the one
    place someone reaches for the newest model by name. The first version of
    this feature shipped `/model claude_cli:opus-5` as its help-text example;
    `opus-5` is neither an alias nor a full name, and the CLI rejected it.

    So nothing is inferred here. The binary's own `--help` is read and the
    lines describing `--model` are returned verbatim. That text is written by
    whoever ships the CLI and updates when they update it, which is the only
    source that stays true.
    """
    exe = shutil.which(command)
    if not exe:
        return Discovery(command, error=f"{command} not installed")
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, "--help",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        return Discovery(command, error=f"could not run {command} --help: {type(exc).__name__}")

    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "--model" not in line or "fallback" in line.lower():
            continue
        # The description usually wraps across the following indented lines.
        block = [line.strip()]
        for cont in lines[i + 1 : i + 6]:
            if not cont.strip() or cont.strip().startswith("-"):
                break
            block.append(cont.strip())
        return Discovery(command, models=[" ".join(block)])
    return Discovery(command, error="no --model option documented in --help")


def rank(models: list[str], hint: str = "") -> list[str]:
    """Order so the interesting names are visible without scrolling.

    Heuristic and honest about it: a provider's list has no released-on date,
    so "newest" cannot be computed. What can be done is float names that share
    a prefix with what you already run, and sink the ones that are plainly not
    chat models — embeddings, whisper, tts, moderation — which otherwise fill
    the first screen of every OpenAI listing.
    """
    noise = ("embed", "whisper", "tts-", "-tts", "moderation", "dall-e", "davinci",
             "babbage", "rerank", "guard")
    stem = (hint or "").split("-")[0].lower()

    def key(name: str) -> tuple[int, int, str]:
        low = name.lower()
        return (
            1 if any(n in low for n in noise) else 0,      # noise last
            0 if stem and low.startswith(stem) else 1,     # your family first
            low,
        )

    return sorted(models, key=key)
