# AETHER — the complete plan

> **The bet:** any model can die mid-task — rate limit, outage, crash, your laptop
> rebooting — and the job still finishes. Not "we retry the request". The *work*
> survives.

| | |
|---|---|
| **Goal** | Open-source project with real users |
| **Excellent at** | Software engineering · research · long autonomous runs |
| **Built by** | AI-driven — this is a backlog of executable work orders, not week estimates |
| **World-best at** | Never losing work |

This is the single authoritative plan: positioning, the honest gap between claim
and reality, 22 work orders across five milestones, and the deferred pile.

---

## 1. Why this bet

Every agent framework competes on capability — more tools, more models, cleverer
prompts. Almost none compete on **durability**, and durability is exactly what
breaks when people run agents on real work for real hours.

Narrow enough to actually win, broad enough to matter:

| Everyone else | AETHER |
|---|---|
| Retries the HTTP request | Checkpoints the *work*, hands off, resumes elsewhere |
| Dies with the process | Survives process, machine and provider death |
| "The agent failed, run it again" | "Provider B picked up from checkpoint 4" |
| Trusts the agent's "done" | Verification gate before `COMPLETED` |

AETHER already has the bones: an authoritative orchestrator, structured handoffs,
checkpoints, SQLite persistence, failover, export/import. **No pivot is needed.**
The work is making the claim true and provable.

The three chosen workloads reinforce it. Long autonomous runs *are* the
durability workload; software engineering and research are the two things people
attempt long runs for.

---

## 2. What must be true before the claim is honest

Measured against the tree today, not aspirationally:

| Failure | Survives? | Gap |
|---|---|---|
| Provider returns an error | ✅ | classified, failed over |
| Provider hangs | ✅ | timeout, then failover |
| Orchestrator restarts | ⚠️ partly | non-terminal tasks requeue, but in-flight agent output is **lost entirely** |
| Machine reboots mid-task | ⚠️ untested | `recover_running` exists; nothing kills a process mid-subtask to prove it |
| Transient network blip | ❌ | **no retry or backoff anywhere** — `tenacity` is declared and never imported |
| Long task exceeds context | ❌ | `ContextBudget` guards chat only; task prompts have no token accounting |
| Move a task to another machine | ⚠️ untested | export/import exists; no round-trip test including the workspace |

Until every ❌ and ⚠️ is ✅ with a test behind it, the tagline is marketing.
**M0 exists to delete this table.**

---

## 3. Milestones

| Milestone | Theme | Ships when | Work orders |
|---|---|---|---|
| **M0** | Earn the claim | Every row above ✅, each covered by a chaos test | WO-01 … WO-06 |
| **M1** | Prove it in public | Guarantees documented; chaos suite in CI | WO-07 … WO-10 |
| **M2** | The two workloads | Real planner; verification that fits; nothing runs unsandboxed | WO-11 … WO-16 |
| **M3** | Scale out | Per-agent worktrees; Postgres/queue option | WO-17 … WO-18 |
| **M4** | Adoption | On PyPI, a demo that lands in 30 seconds, contributor on-ramp | WO-19 … WO-22 |

---

## 4. Work orders

Each is self-contained: files, the problem, what to build, and acceptance
criteria written as assertions. "Done" means demonstrably met — a test, or a
command whose output is shown. ⭐ marks the ones that carry the bet.

---

### M0 — Earn the claim

#### WO-01 · Chaos harness: kill the orchestrator mid-task
**Files:** new `tests/test_durability.py`
**Build:** start a task with a slow mock agent in a subprocess, `SIGKILL` it
mid-subtask, then start a fresh app over the same `data_dir` and let it recover.
**Acceptance:**
- reaches `COMPLETED` after recovery with no human input
- no subtask runs twice (one `subtask_complete` per subtask on the timeline)
- no subtask stranded `RUNNING` — the reclaim path exists; this proves it
- 10 consecutive runs with randomised kill offsets

#### WO-02 · Never lose in-flight agent output ⭐
**Files:** `adapters/cli_adapter.py`, `adapters/api_adapter.py`,
`adapters/opencode_adapter.py`, `core/orchestrator.py`
**Problem:** adapters buffer the whole response in memory. Kill the process at
minute nine of a ten-minute agent and *everything* is gone. This is the single
largest hole in the durability claim.
**Build:** stream output to `workspace/<task>/logs/exec-<execution_id>.log` as it
arrives. On recovery, if a log exists for an unfinished execution, parse it into
a partial handoff instead of restarting from zero.
**Acceptance:**
- kill mid-execution → the log holds everything received up to that moment
- recovery yields a `PARTIAL` handoff whose `completed` reflects salvaged work
- a test asserts the resumed task does *not* redo the salvaged portion

#### WO-03 · Retry with backoff before burning a failover
**Files:** `adapters/base.py`, `core/orchestrator.py`, `config/settings.py`
**Problem:** `network_retries` is configured and never read; `tenacity` is
declared and never imported. One blip costs a failover *and* a circuit-breaker
failure. While here, audit every setting: `half_open_max_calls` is also dead.
Configuration that does nothing is worse than none — it implies a guarantee.
**Build:** jittered exponential backoff for `NETWORK` and `TIMEOUT` only — never
`AUTH`, `QUOTA` or `POLICY_DENIED`, which retrying cannot fix. Wire or delete
every unused knob.
**Acceptance:**
- adapter failing twice then succeeding completes on the same provider, with no
  failover event and no circuit-breaker increment
- an `AUTH` failure fails over immediately, zero retries
- `network_retries: 0` disables it
- every setting in `config/aether.default.yaml` has a test proving it changes behaviour

#### WO-04 · Context budgeting for agent prompts ⭐
**Files:** `core/context.py`, `domain/models.py` (`to_prompt_block`), `core/orchestrator.py`
**Problem:** `ContextBudget` exists and is used only by chat. Task prompts use
fixed slices (last 20 decisions, 50 files) with no token accounting, so a long
task silently exceeds the window and fails `CONTEXT` — repeatedly, since the
retry sends the same oversized package.
**Build:** budget the package against the *target provider's* window; compact
decisions, file index and last handoff to fit — oldest and least relevant first,
always preserving the assignment and latest handoff.
**Acceptance:**
- a task with 500 indexed files and 200 decisions completes with no `CONTEXT` failure
- the built package measures under the soft limit for every provider in the pool
- compaction is recorded on the timeline, never silent

#### WO-05 · Cross-machine resume, proven
**Files:** `core/export.py`, new test
**Build:** `export_bundle` already zips the workspace — prove the round trip.
Export a half-finished task, import into a *different* `data_dir`, resume there.
**Acceptance:**
- imported task resumes and completes; pre-move artifacts present
- handoff trail and checkpoints intact
- the README snippet showing the two commands is covered by the test

#### WO-06 · The small defects blocking a clean claim
**Files:** `core/verification.py`, `core/router.py`, `adapters/opencode_adapter.py`, `core/chat_session.py`
Four cheap ones, each with a test:
- kill the child on verification timeout (currently orphaned)
- cache `health_check` with a short TTL — routing issues a live HTTP request per
  candidate *per subtask*, so a 5-subtask task probes each provider 5 times
- Open Code: long prompts via stdin/temp file, not argv (Windows ~32 KB limit)
- `chat_stream` must not double-execute on an empty-but-valid reply

---

### M1 — Prove it in public

#### WO-07 · Publish durability guarantees
**Files:** new `docs/DURABILITY.md`, README section
**Build:** the §2 table with every row ✅, each linked to the test that proves it.
State the limits honestly too: single machine, SQLite single-writer,
last-writer-wins JSON state.
**Acceptance:** every row cites a test id. No claim without one.

#### WO-08 · Chaos suite in CI
**Files:** `.github/workflows/ci.yml`
**Build:** nightly job running WO-01/02/05 with randomised kill points, Linux and
Windows.
**Acceptance:** green nightly; a failure opens an issue automatically.

#### WO-09 · `aether doctor --durability`
**Build:** report what *this install* can survive — git present for checkpoints,
workspace writable, state dir lockable, DB reachable.
**Acceptance:** on a deliberately broken install (read-only workspace) it names
the specific guarantee that is void.

#### WO-10 · Make the tests assert mechanisms, not tautologies
**Files:** `tests/test_failure_scenarios.py`
**Problem:** several tests assert `status in {COMPLETED, FAILED}` — which
`wait_terminal` already guarantees. They pass whether or not failover works. On a
project selling reliability, tests that cannot fail are worse than no tests.
**Acceptance:** each asserts the actual mechanism — which provider ran, that a
failover event fired, that a checkpoint exists — and each is checked to fail when
the mechanism is disabled.

---

### M2 — The two workloads

#### WO-11 · A planner worth the name ⭐
**Files:** `core/workflow.py`
**Problem:** `_plan_solo` picks the task shape with
`if any(k in prompt for k in ("build", "implement", "code", ...))`. "Refactor the
auth module" matches none of them and falls to the generic path. This is the
ceiling on output quality.
**Build:** an LLM planner emitting a subtask DAG against a JSON schema, validated
before use; the keyword planner remains the deterministic fallback when no
provider is available or validation fails. **The orchestrator still owns
execution — only the plan comes from a model.**
**Acceptance:**
- "Refactor the auth module to use dependency injection" yields a sensible
  multi-step DAG with dependencies
- an invalid or hallucinated plan falls back to keywords and records why
- planning runs against a budget and cannot loop

#### WO-12 · Verification that fits the task
**Files:** `core/verification.py`
**Problem:** checks are fixed. A research task is "verified" by the existence of
a `.md` file.
**Build:** strategies chosen by task type; acceptance criteria recorded on the
task at plan time and checked at the gate. Research: sources present, claims
attributed, output addresses the question. Code: builds, tests pass, no syntax
errors.
**Acceptance:** a research task with an unsourced output fails verification.

#### WO-13 · Sandbox the verification run ⭐
**Files:** `core/verification.py`, `security/sandbox.py`
**Problem:** the verification gate runs `pytest` **on agent-written code with no
isolation**. That is arbitrary code execution as part of the step whose whole
purpose is not trusting the agent. `ExecutionSandbox` gates *whether* it runs,
not *what it can do*.
**Build:** run verification in a container (Docker/Podman) or a restricted
subprocess, opt-in via config, with today's behaviour kept as an explicitly
documented "trusted workspace" mode.
**Acceptance:** a workspace whose test file tries to read outside the workspace
or open a socket fails closed under the sandboxed mode.

#### WO-14 · Live agent output
**Files:** `core/orchestrator.py`, `web/src/App.tsx`
**Problem:** `agent_stream` is emitted **once, after** `execute()` returns. On a
long autonomous run the UI shows nothing for ten minutes, then everything — for
this project's target workload that is the difference between usable and not.
**Build:** consume `adapter.stream()` where supported and emit incrementally.
Pairs with WO-02, which already writes that stream to disk.
**Acceptance:** dashboard and CLI show tokens as they arrive; killing the process
mid-stream still leaves the partial log.

#### WO-15 · Real token counting
**Files:** new `core/tokens.py`, `core/context_budget.py`, `core/budget.py`, adapters
**Problem:** six places estimate tokens as `len(text) // 4`. Budgets, quota and
compaction thresholds all inherit that error, which is large for code and for
non-English text — and WO-04 depends on the number being right.
**Build:** a `TokenCounter` per provider family (`tiktoken` where applicable,
chars/4 as the documented fallback), used everywhere.
**Acceptance:** counts within 5% of the provider's reported usage on a fixture.

#### WO-16 · Per-provider cost
**Files:** `config/settings.py`, `core/budget.py`, `core/metrics.py`
**Problem:** `usd_per_1k_tokens` is one global number, so a task mixing an
expensive and a cheap model reports fiction.
**Build:** input/output rates in provider config; cost broken down per provider.
**Acceptance:** `aether metrics` shows per-provider spend; a mixed-provider task
reports a total matching hand-calculation.

---

### M3 — Scale out

#### WO-17 · A git worktree per agent
**Files:** `core/workspace.py`, `core/orchestrator.py`
**Problem:** parallel agents share one working tree, so the post-execution phase
is serialised under a lock — parallel mode is parallel only during model calls.
**Build:** `git worktree` per agent branch; merge as today, conflicts still spawn
a resolver subtask.
**Acceptance:** two agents edit different files simultaneously; the memory lock
around the git section can be removed; conflict detection still passes.

#### WO-18 · Postgres and a durable queue
**Build:** exercise the "portable to Postgres" claim in CI; move control-plane
state out of JSON when Postgres is configured.
**Acceptance:** full suite green against Postgres; two orchestrators over one
database never double-run a task.

---

### M4 — Adoption

#### WO-19 · Ship it
PyPI release so it is `pipx install aether-orchestrator`; tag `v0.1.0`; a
`CHANGELOG` entry that reads like release notes.

#### WO-20 · A demo that lands in 30 seconds
An asciinema recording in the README showing what nobody else shows: a task
running, a provider killed mid-flight, another picking up from the checkpoint,
the task completing. **That recording is the entire pitch.**

#### WO-21 · Contributor on-ramp
`CONTRIBUTING.md`, issue and PR templates, a `good first issue` pass — WO-06 is
four of them.

#### WO-22 · Say one thing, everywhere
`README` says "not a chatbot"; `docs/PRODUCT.md` says "an LLM router that works
like an AI agent terminal". Pick the durability framing; make every surface agree.

---

## 5. Sequencing

```
M0  WO-01 → WO-02 → WO-03 → WO-04 → WO-05 → WO-06     the claim becomes true
M1  WO-07 → WO-08 → WO-09 → WO-10                      the claim becomes provable
M2  WO-11 → WO-12 → WO-13 → WO-14 → WO-15 → WO-16      the work gets good
M3  WO-17 → WO-18                                      it scales
M4  WO-19 → WO-20 → WO-21 → WO-22                      people find it
```

Two dependencies worth respecting: **WO-15 before WO-04 lands for real** (context
budgeting is only as good as the token count), and **WO-02 before WO-14** (the
streaming work is shared).

**If only three get done:** WO-02, WO-04, WO-13. The first two make "never loses
work" true in the two most common failure modes; the third closes the one
genuinely dangerous hole.

**Do not start M4 before M1.** Launching on a durability claim with no proof
behind it is the one mistake that cannot be undone.

---

## 6. How to know it worked

| Signal | Target |
|---|---|
| Chaos suite | 100% pass, nightly, both platforms |
| Tasks completing without human intervention | > 95% on the mock; then measured on real providers |
| Work lost to a mid-task kill | 0 bytes — partial output always salvaged |
| Cross-machine resume | works first time, proven by test |
| `pipx install` → first completed task | under 5 minutes |
| Token/cost accuracy | within 5% of provider-reported |

---

## 7. Deferred

Real demand should pull these, not enthusiasm:

- **Redis queue and multi-machine workers** — the interfaces already anticipate it
- **MCP tool support**, so agents call tools through AETHER
- **Provider capability auto-probing** instead of hand-tuned scores
- **Scheduled / recurring tasks**
- **Vite 5 → 6 upgrade** — dev-server-only advisories, and the dashboard is now
  built and served by the API, so the dev server is out of the normal path

## 8. Not doing

- **Rewriting onto a graph framework** (LangGraph et al). The orchestrator *is*
  the product; adopting someone else's control flow inverts the core principle.
- **Hosted / multi-tenant mode.** A different product. Revisit only if users ask.
- **Chasing model capability.** Providers do that. AETHER's job is to survive them.
- **Auto-approving destructive commands.** The approval gate is the point.
- **Scraping subscription auth into API calls.** Non-negotiable.
