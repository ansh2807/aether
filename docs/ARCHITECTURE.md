# AETHER — Architecture Document

**Version:** 0.1.0  
**Status:** Approved for implementation (engineering defaults applied)  
**Date:** 2026-08-09

---

## 1. Requirements Analysis

### 1.1 What AETHER Is

AETHER is a **deterministic multi-agent AI operating system / orchestrator**. Users submit goals; a central Orchestrator owns task state, agent selection, handoffs, failover, verification, and artifacts. Providers (API or CLI) are pluggable adapters—not the control plane.

### 1.2 What AETHER Is Not

- Not a multi-agent chat room where models talk freely
- Not a hard-coded provider switcher
- Not a fake demo that pretends CLI agents are integrated without real subprocess boundaries
- Not a system that bypasses provider auth, quotas, or subscription rules

### 1.3 Contradictions & Technical Limitations

| Issue | Resolution |
|-------|------------|
| Many “agents” are interactive CLIs (Claude Code, Codex, etc.) with no stable machine API | Support **CLI adapters** via controlled subprocess; parse stdout; never claim full parity with API adapters |
| Provider capabilities change constantly | Capabilities are **config-driven**, not hard-coded scores |
| Cannot legally convert subscriptions to APIs | Never reverse-engineer auth; use env/keychain/already-authenticated CLI only |
| Full multi-agent collaboration can thrash | Orchestrator enforces **structured messages**, max rounds, budgets, DAG concurrency |
| Shared filesystem races | Git branches/checkpoints per agent action; conflict detection on merge |
| Redis/Postgres/Docker not always available | **SQLite + asyncio in-process queue** by default; interfaces allow Postgres/Redis later |
| “Hermes”, “Antigravity”, etc. may lack public integration | Register as **UNAVAILABLE / ADAPTER_REQUIRED** until real adapter ships |
| Agents claim “done” incorrectly | **Verification engine** is mandatory gate before COMPLETED |
| Context window limits | **Structured task memory** + scoped context packages, not full history dumps |

### 1.4 Non-Negotiable Invariants

1. **Orchestrator is authoritative** — agents never own global state.
2. **Persistence first** — tasks survive process/machine restart.
3. **No single-provider lock-in** — failover is a first-class path.
4. **Handoffs are formal** — structured reports, not chat logs.
5. **Secrets never enter task memory**.
6. **Verification before trust**.
7. **Adapters are the only provider touchpoint**.

---

## 2. Technology Stack (Decisions)

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Language | Python 3.11+ | Async, subprocess, packaging maturity |
| API | FastAPI + WebSockets | Clean REST + realtime dashboard |
| Persistence | SQLite (aiosqlite) + SQLAlchemy 2.x | Zero-ops dev; portable to PostgreSQL |
| Queue | asyncio worker pool | No Redis required; `TaskQueue` interface for Redis later |
| Config | YAML + Pydantic Settings | Human-editable provider profiles |
| CLI | Typer + Rich | Production-quality terminal UX |
| Models | Pydantic v2 | Validation of handoffs, messages, tasks |
| Git | subprocess Git | Checkpoints & agent branches |
| Frontend | Vite + React + TypeScript | Fast dashboard without heavy framework lock-in |
| Logging | structlog JSON | Observability & audit |
| Packaging | pyproject.toml / hatchling | `pip install -e .` + `aether` entrypoint |

**Deferred (interfaces ready):** Redis queue, PostgreSQL, Docker Compose, secret manager backends (OS keychain via `keyring` optional).

---

## 3. Component Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         USER (CLI / Web UI)                              │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────────────┐
│  API Gateway (FastAPI)  │  CLI (Typer)  │  WebSocket Event Bus          │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────────────┐
│                         ORCHESTRATOR (core)                              │
│  plan → route → execute → handoff → verify → complete / failover        │
└───┬─────────┬─────────┬─────────┬─────────┬─────────┬─────────┬────────┘
    │         │         │         │         │         │         │
┌───▼───┐ ┌───▼───┐ ┌───▼───┐ ┌───▼───┐ ┌───▼───┐ ┌───▼───┐ ┌───▼───┐
│ Task  │ │Router │ │Memory │ │Handoff│ │ Check │ │Verify │ │Workflow│
│Manager│ │       │ │Context│ │Manager│ │point  │ │Engine │ │  DAG   │
└───┬───┘ └───┬───┘ └───┬───┘ └───┬───┘ └───┬───┘ └───┬───┘ └───┬───┘
    │         │         │         │         │         │         │
┌───▼─────────▼─────────▼─────────▼─────────▼─────────▼─────────▼───────┐
│                    SHARED PERSISTENCE LAYER                              │
│  TaskStore │ MemoryStore │ MessageBus │ AuditLog │ ArtifactIndex       │
│  (SQLite)  │ (SQLite)    │ (SQLite)   │ (JSONL)  │ (filesystem)        │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼─────────────────────────────────────┐
│  AgentRegistry │ QuotaManager │ HealthManager │ CircuitBreaker │ Sandbox │
└───────────────────────────────────┬─────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼─────────────────────────────────────┐
│                         ADAPTER LAYER                                    │
│  AgentAdapter interface                                                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────────┐│
│  │ CLI Base │ │ API Base │ │ Mock*    │ │ Future…  │ │ Stub/Unavail.  ││
│  └────┬─────┘ └────┬─────┘ └──────────┘ └──────────┘ └────────────────┘│
│       │            │                                                      │
│  claude,codex…  openai,gemini,grok…                                      │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼─────────────────────────────────────┐
│  SHARED WORKSPACE: /workspace/{task_id}/  (git-backed)                   │
│  artifacts/ logs/ checkpoints/ memory/                                    │
└─────────────────────────────────────────────────────────────────────────┘

* Mock adapter: tests only; never registered as a real provider by default.
```

---

## 4. Data Model

### 4.1 Task

```
Task
  task_id: str                 # TASK-YYYYMMDD-XXXXX
  original_prompt: str
  normalized_goal: str
  status: TaskStatus
  priority: int
  mode: solo | collab | debate | parallel
  created_at / updated_at / completed_at
  active_agent: Optional[str]
  participating_agents: list[str]
  progress: float              # 0.0–1.0
  parent_task_id: Optional[str]
  workspace_path: str
  approval_level: auto | safe | approval
  metadata: dict
```

### 4.2 Subtask (DAG node)

```
Subtask
  subtask_id, task_id, title, description
  status, required_capabilities: list[str]
  depends_on: list[subtask_id]
  assigned_agent, role
  result_summary, artifacts
```

### 4.3 TaskMemory (structured shared memory)

```
TaskMemory
  task_id
  goal
  current_state
  decisions: list[Decision]
  files_index: list[str]
  known_issues: list[str]
  last_agent, last_action, next_action
  open_questions: list[str]
  updated_at
```

### 4.4 HandoffReport

```
HandoffReport
  handoff_id, task_id, from_agent, to_agent (optional)
  status: COMPLETE | PARTIAL | FAILED | BLOCKED
  requested, completed: list[str], remaining: list[str]
  files_changed, decisions, tests_executed, test_results
  known_bugs, assumptions, recommended_next_action
  artifacts, logs, raw_output_ref
  created_at
```

### 4.5 AgentMessage

```
AgentMessage
  message_id, task_id
  sender, recipient           # agent_id | orchestrator | user
  message_type: TASK|PLAN|QUESTION|PROPOSAL|CRITIQUE|ARTIFACT|
                HANDOFF|ERROR|TEST_RESULT|DECISION|REQUEST_REVIEW|COMPLETION
  content, referenced_artifacts, priority, timestamp
```

### 4.6 Checkpoint

```
Checkpoint
  checkpoint_id, task_id, agent_id
  git_commit, state_summary
  tests_summary, next_action
  memory_snapshot_ref, created_at
```

### 4.7 Provider / Capability Profile

```
ProviderProfile
  provider_id, display_name
  adapter_type: cli | api | unavailable
  enabled, command / base_url
  capabilities: dict[str, int]   # 0–10 scores from config
  limits: requests/day, rpm, cooldown
  roles_preferred: list[str]
```

### 4.8 VerificationResult

```
VerificationResult
  result_id, task_id, subtask_id?
  status: PASSED | FAILED | PARTIAL
  checks: list[{name, status, detail}]
  created_at
```

### 4.9 Usage / Health / Circuit

```
UsageRecord, HealthStatus, CircuitState(CLOSED|OPEN|HALF_OPEN)
```

---

## 5. Agent Adapter Interface

```python
class AgentAdapter(Protocol):
    provider_id: str

    async def initialize(self) -> None: ...
    async def authenticate(self) -> AuthStatus: ...
    async def health_check(self) -> HealthStatus: ...
    async def get_capabilities(self) -> CapabilityProfile: ...
    async def execute(self, request: ExecutionRequest) -> ExecutionResult: ...
    async def stream(self, request: ExecutionRequest) -> AsyncIterator[StreamChunk]: ...
    async def cancel(self, execution_id: str) -> None: ...
    async def get_usage(self) -> UsageSnapshot: ...
    async def get_limits(self) -> LimitSnapshot: ...
    async def shutdown(self) -> None: ...
```

**Registration path for a new provider:**

1. Implement `AgentAdapter` (or subclass `CliAgentAdapter` / `ApiAgentAdapter`)
2. Define capability profile in YAML
3. Add config block under `providers:`
4. Register in `AgentRegistry` (entry-point or import path)

No orchestrator rewrite required.

---

## 6. Task Lifecycle

```
CREATED
  → PLANNING          # orchestrator decomposes goal
  → QUEUED            # waiting for capacity
  → RUNNING           # agent(s) executing
  → WAITING           # blocked on approval / dependency
  → HANDOFF           # formal handoff processing
  → REVIEW            # peer/orchestrator review
  → TESTING           # verification engine
  → PAUSED            # user pause
  → FAILED            # terminal failure after retries
  → COMPLETED         # verified success
  → CANCELLED         # user cancel
```

**Transitions** are only applied by `TaskManager` under Orchestrator control. Illegal transitions raise `InvalidStateTransition`.

---

## 7. Handoff Protocol

1. Agent execution ends (success, partial, error, cancel, timeout).
2. Adapter normalizes raw output → attempts to extract/construct `HandoffReport`.
3. If malformed → Orchestrator synthesizes minimal handoff from memory + logs (`status=PARTIAL`, `known_bugs=["malformed_handoff"]`).
4. `HandoffManager.validate()` + persist.
5. `CheckpointManager.create()` (git commit if workspace dirty).
6. `ContextManager.build_package()` for next agent.
7. `AgentRouter.select()` unless task complete.
8. MessageBus emits `HANDOFF` event for UI/CLI.

---

## 8. Failover Algorithm

```
on_failure(task, agent, error):
  1. classify(error) → TIMEOUT | CRASH | AUTH | RATE_LIMIT | QUOTA |
                       NETWORK | CONTEXT | INVALID_RESPONSE | UNKNOWN
  2. record_failure(agent, error)
  3. circuit_breaker.record_failure(agent)
  4. create_checkpoint(task)  # best-effort
  5. synthesize_handoff_from_partial_state()
  6. if task.retries >= max_retries: mark FAILED; notify user; return
  7. candidates = router.select_failover(task, exclude=[agent] + open_circuits)
  8. if no candidates: status=WAITING or FAILED; notify user
  9. else: assign next; status=HANDOFF→RUNNING; continue from checkpoint
```

**User experience:** task timeline shows agent switch; status remains continuous.

---

## 9. Security Model

| Control | Implementation |
|---------|----------------|
| Secrets | Env vars / optional keyring; never in TaskMemory or logs |
| Redaction | `SecretRedactor` on all log sinks |
| Shell | Allowlist/denylist + approval gates, enforced by `ExecutionSandbox` before every `create_subprocess_exec` |
| Workspace | `check_workspace` rejects any execution `cwd` resolving outside `workspace_root` |
| Subprocess | timeouts, max output size, no shell=True when avoidable |
| Permissions | `approval_level`: auto / safe / approval |
| Audit | Append-only JSONL audit log |
| Credentials in CLI adapters | Inherit process env of already-authenticated CLI; AETHER does not scrape credential files into memory |

### Enforcement points

`ExecutionSandbox` is consulted at every point where AETHER itself starts a process:

| Call site | Checked |
|-----------|---------|
| `CliAgentAdapter.execute` | `command + args`, execution `cwd` |
| `OpencodeCliAdapter.execute` | `opencode run --model <model>`, execution `cwd` |
| `VerificationEngine.verify_task` | `pytest` invocation, workspace `cwd` |

Denial produces `FailureClass.POLICY_DENIED`, which the orchestrator handles as an
operator decision: no circuit-breaker penalty, no failover to another provider, task
fails with the reason recorded on the timeline (`policy_denied`).

**Boundary.** Policy governs the command AETHER chose to launch — never the prompt,
which is agent input. It is not a sandbox around a running agent CLI; once such a
tool starts, its own permission model applies. `WorkspaceManager` and
`CheckpointManager` git calls are orchestrator-internal and are not policy-gated.

---

## 10. Repository Structure

```
aether/
├── docs/
│   ├── ARCHITECTURE.md
│   ├── HANDOFF_PROTOCOL.md
│   ├── FAILURE_SCENARIOS.md
│   └── ROADMAP.md
├── config/
│   └── aether.default.yaml
├── src/aether/
│   ├── __init__.py
│   ├── __main__.py
│   ├── app.py                 # composition root
│   ├── config/
│   ├── domain/                # pydantic models, enums
│   ├── interfaces/            # protocols / ABCs
│   ├── storage/               # SQLite repositories
│   ├── core/                  # orchestrator, router, etc.
│   ├── adapters/
│   ├── api/                   # FastAPI routes + WS
│   ├── cli/
│   ├── security/
│   └── observability/
├── web/                       # React dashboard
├── tests/
├── workspace/                 # runtime (gitignored)
├── data/                      # sqlite + logs (gitignored)
├── pyproject.toml
├── README.md
└── .gitignore
```

---

## 11. Implementation Roadmap

| Phase | Deliverable | Exit criteria |
|-------|-------------|----------------|
| 1 | Architecture docs | This document |
| 2 | Interfaces + models + config | Importable package |
| 3 | Task/memory/checkpoint stores | Persist across restart |
| 4 | Adapters + registry + health/quota/circuit | Pluggable providers |
| 5 | Orchestrator + router + handoff + verify | End-to-end mock run |
| 6 | CLI + API + security | `aether task "..."` works |
| 7 | UI + failure tests + packaging | Dashboard + scenario suite |

---

## 12. Engineering Defaults (no user approval needed)

1. SQLite default; Postgres via connection string later  
2. In-process asyncio workers (no Redis required)  
3. Mock adapter **only** for tests / demo mode flag  
4. Real adapters: CLI generic + OpenAI-compatible API + stubs for others  
5. Approval level default: `safe`  
6. Debate max rounds: 3; max agents: 4  
7. Task ID format: `TASK-YYYYMMDD-XXXXX`  
8. Workspace root: `./workspace` under data dir  

---

## 13. Success Criteria Mapping

| Criterion | Component |
|-----------|-----------|
| Understand task | Planner in Orchestrator |
| Break into subtasks | WorkflowEngine DAG |
| Select agents | AgentRouter |
| Assign work | Orchestrator + TaskManager |
| Shared state | TaskMemory + MessageBus |
| Collaborate | collab/debate modes + structured messages |
| Checkpoints | CheckpointManager + Git |
| Failures + switch | Failover + CircuitBreaker |
| Continue from state | Handoff + ContextManager |
| Test + fix loop | VerificationEngine |
| Observability | Audit + timeline + structured logs |
