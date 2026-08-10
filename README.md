# AETHER — Universal Multi-Agent AI Orchestrator

**AETHER** is a production-oriented multi-agent **AI operating system / orchestrator**.  
It is **not** a chatbot and **not** a free-form multi-agent chat room.

A **deterministic central Orchestrator** owns:

- task state & lifecycle  
- agent selection & routing  
- structured handoffs  
- checkpoints & recovery  
- failover across providers  
- verification  
- quotas, circuit breakers, audit  

Providers (OpenAI, Claude CLI, Codex CLI, Gemini, Grok, …) plug in through a common **AgentAdapter** interface (API or CLI). Adding a provider should only require: adapter + capabilities + config + registration.

---

## Architecture (summary)

```
USER → CLI / UI → ORCHESTRATOR → TASK MANAGER → AGENT ROUTER
        → Adapters (API | CLI | Unavailable) → Shared workspace (git)
        → Verification → Final result
```

Full design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)

---

## Stack

| Layer | Choice |
|-------|--------|
| Language | Python 3.11+ |
| API | FastAPI + WebSockets |
| DB | SQLite (SQLAlchemy async) — portable to Postgres |
| CLI | Typer + Rich |
| Dashboard | React + Vite + TypeScript |
| Queue | In-process asyncio workers |

---

## Quick start

```bash
cd aether
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux/macOS
# source .venv/bin/activate

pip install -e ".[dev]"

# Smoke: mock agent completes a full task lifecycle
aether task "Build a small Python hello module"

# System status / provider health
aether status
aether agents health
aether providers

# Interactive
aether

# API + dashboard backend (prints an API token — the dashboard link includes it)
aether serve
# → http://127.0.0.1:7420

# Dashboard (optional) — build once, then `aether serve` hosts it
cd web && npm install && npm run build
# → served at http://127.0.0.1:7420
```

> **The dashboard needs the API token from `aether serve`.**
> Open the link it prints — `http://127.0.0.1:7420/?token=…` — not a bare
> `localhost:7420`. The token is stored in `localStorage` on first load, so
> later visits work without the query string. Without it the page loads but
> every panel shows *"API token missing or invalid"*. See
> [API authentication](#api-authentication).

Default config enables the **mock** adapter for offline development and tests.  
Real providers are **disabled** until you enable them and supply keys/CLIs.

> The mock agent fabricates plausible output and always reports success. It is
> routable **only while no real provider is enabled** — it will never quietly
> pick up work a real provider failed at. Override with
> `routing.allow_testing_fallback: true` if you actually want that.

---

## Configuration

Copy and edit:

```bash
copy config\aether.default.yaml config\aether.yaml   # Windows
# cp config/aether.default.yaml config/aether.yaml
```

Or set `AETHER_CONFIG` / place `~/.aether/config.yaml`.

### Enable OpenAI-compatible API

```yaml
providers:
  openai:
    enabled: true
    adapter: api
    base_url: https://api.openai.com/v1
    model: gpt-4o
    api_key_env: OPENAI_API_KEY
```

```bash
set OPENAI_API_KEY=sk-...
```

### Enable CLI agent (already authenticated on machine)

```yaml
providers:
  claude_cli:
    enabled: true
    adapter: cli
    command: claude
    args: []
    # optional: prompt_mode: stdin | arg | flag
```

AETHER **never** converts subscriptions into APIs or scrapes credentials into task memory.  
CLI adapters use subprocess + existing machine auth only.

### Unavailable providers

`kimi`, `hermes`, `antigravity` ship as `adapter: unavailable` with status **ADAPTER_REQUIRED** until you implement a real adapter.

---

## CLI reference

```text
aether                              # interactive
aether run "Build X"                # create + run
aether task create "Build X"        # same
aether task list
aether task inspect TASK-...
aether task pause|resume|cancel TASK-...
aether task export TASK-... -o out.json
aether task import out.json [--resume]
aether task trace TASK-...
aether agents
aether agents health
aether agents test mock
aether providers
aether status
aether doctor                       # system diagnostics
aether metrics [TASK-...]
aether approvals                    # pending human gates
aether approvals grant apr-...
aether approvals deny apr-...
aether logs [TASK-...]
aether checkpoint list TASK-...
aether serve
```

Modes (interactive prefixes): `/collab`, `/debate`, `/parallel`

### Extended capabilities

| Feature | Description |
|---------|-------------|
| **Approval queue** | Human-in-the-loop gates (`WAITING` until grant/deny) |
| **Budgets** | Per-task token/request/cost caps |
| **Event bus** | Live events + ring buffer; WebSocket broadcast |
| **Metrics / traces** | Success rates, latency, per-task traces |
| **Git agent branches** | Parallel/collab work on branches + merge conflict subtasks |
| **Export / import** | Task continuity across restarts/machines |
| **Doctor** | Provider/CLI/secret/env diagnostics |
| **Plugins** | `adapter_class: mypkg:MyAdapter` or entry points `aether.adapters` |

---

## Task lifecycle

`CREATED → PLANNING → QUEUED → RUNNING → HANDOFF → REVIEW/TESTING → COMPLETED`  
Also: `WAITING`, `PAUSED`, `FAILED`, `CANCELLED`

State persists in SQLite + workspace so tasks survive **orchestrator and machine restarts**.

Control-plane state that the CLI and the server must both see — pending
approvals, metrics and traces, budgets, provider usage and cooldowns — is stored
as JSON under `data/state/`. That is what makes `aether approvals grant …` work
against a task started by `aether serve`, and what stops a restart from
forgetting quota already spent. Daily request counters roll over at UTC midnight.

---

## Handoff protocol

Agents produce structured handoff reports (STATUS / COMPLETED / REMAINING / FILES / …).  
Malformed output is **synthesized** into a partial handoff — the next agent continues from checkpoint, not from zero.

See [`docs/HANDOFF_PROTOCOL.md`](docs/HANDOFF_PROTOCOL.md).

---

## Failover

On timeout, crash, auth, rate limit, quota, network, context overflow, etc.:

1. Classify failure  
2. Checkpoint + handoff  
3. Open/trip circuit if needed  
4. Route to next capable healthy agent  
5. Continue with context package  

Documented scenarios: [`docs/FAILURE_SCENARIOS.md`](docs/FAILURE_SCENARIOS.md)

---

## Security

- Secrets only via env / OS (never task memory)
- Log redaction (logs, audit trail, stored messages and timeline details)
- Subprocess timeouts & output caps
- Token-gated local API (below)

### API authentication

`aether serve` exposes an API that creates tasks, spawns subprocesses and stores
provider keys, and it has no user model. Binding to loopback stops remote hosts
but not other local processes or a web page in your browser, so the API is
**token-gated by default**.

The token is generated on first run into `data/api_token` (or set
`AETHER_API_TOKEN`). `aether serve` prints it along with a dashboard link that
carries it; the dashboard stores it in `localStorage` for later reloads.

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:7420/api/status
```

`X-Aether-Token` and `?token=` are also accepted — the latter for WebSocket and
EventSource, which cannot set headers. Only `/api/health` is public. Disable with
`server.require_auth: false` if you have another control in front of it.

CORS lists only `server.cors_origins`; there is deliberately no wildcard origin,
since `*` combined with credentials lets any page in your browser drive the API.

### Dashboard without a dev server

`aether serve` hosts the built dashboard from `web/dist` at `/`, so the normal
workflow needs no Vite dev server:

```bash
cd web && npm install && npm run build   # once, and after UI changes
aether serve                             # prints http://127.0.0.1:7420/?token=...
```

This matters for more than convenience. Every current npm advisory in this
project's front-end tree is **dev-server only** (path traversal in Vite's
optimized-deps handling, a `server.fs.deny` bypass on Windows, an NTLM hash
disclosure via `launch-editor` on Windows). None of them are reachable if you
never run `npm run dev`. Building and serving statically removes that surface
instead of chasing versions.

Static files are public; every byte of *data* still requires the token. Point
`server.static_dir` elsewhere to serve a dashboard built somewhere else.

Use `npm run dev` only when actively editing the UI and you want HMR — and
close it when you're done.

### Execution policy (`ExecutionSandbox`)

Every subprocess AETHER spawns — provider CLIs and verification commands — is
checked against policy **before** it starts:

- **Command denylist** — substring match, always enforced at every approval level
- **Approval levels** — `auto` (denylist only) · `safe` (gate destructive ops:
  delete/install/push/deploy/…) · `approval` (gate everything but read-only commands)
- **Workspace isolation** — the execution `cwd` must resolve inside `workspace_root`

A blocked execution returns `FailureClass.POLICY_DENIED`. The orchestrator treats
that as an operator decision, not a provider fault: it does **not** count against
the provider's circuit breaker and does **not** fail over to another provider to
run the same denied command. The task fails with the policy reason in its timeline.

Grant an operation with `aether approvals grant apr-...`, `POST /api/sandbox/grant`,
or by adding a prefix to `security.command_allowlist_prefixes`.

**Scope — read this before relying on it.** Policy is evaluated against the command
AETHER *chose to run* (the configured `command` plus its static `args`), never against
the prompt, which is agent input rather than an instruction to the shell. It is a
guard on AETHER's own process launches, **not** a sandbox around the agent: once an
autonomous CLI such as Claude Code or Open Code is running, whatever it does next is
governed by that tool's own permission model, not by AETHER. Use the provider's
sandboxing, and run untrusted work in a VM or container.

---

## Adding a provider

1. Implement `AgentAdapter` (or subclass `CliAgentAdapter` / `ApiAgentAdapter`)  
2. Add capability scores in YAML  
3. Enable under `providers:`  
4. Register via config (auto-built by `AgentRegistry`)  

No orchestrator rewrite.

---

## Tests

```bash
pytest -q
```

Includes unit tests, end-to-end mock orchestration, failover, verification, cancel/pause, and the documented failure scenarios.

---

## Repository layout

```text
aether/
  docs/                 Architecture & protocols
  config/               Default YAML
  src/aether/
    adapters/           CLI, API, mock, unavailable, registry
    core/               Orchestrator, router, handoff, verify, …
    storage/            SQLite repositories
    api/                FastAPI + WebSocket
    cli/                Typer CLI
    security/           Redaction + sandbox
  web/                  React dashboard
  tests/
```

---

## Important principles

1. **Orchestrator is authoritative** — agents never own global state.  
2. **No single-provider lock-in** — failover is first-class.  
3. **Do not fake integrations** — unavailable = ADAPTER_REQUIRED.  
4. **Verification before trust** — “done” is not enough.  
5. **Respect provider auth, quotas, and terms.**  

---

## License

Apache-2.0
