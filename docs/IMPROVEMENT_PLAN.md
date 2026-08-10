# AETHER improvement plan

Written after a full read of the tree, with each claim checked against the code
rather than assumed. Items are ordered by **risk removed per hour spent**, not by
how interesting they are.

Sizes: **S** ≈ under a day · **M** ≈ 1–3 days · **L** ≈ a week or more.

---

## Where it stands today

Solid: the orchestrator genuinely owns state; adapters are a real boundary;
failover, verification, budgets, quotas and audit all exist and are exercised by
116 tests on Linux and Windows.

The gap is not architecture — it is that several parts are **declared but not
wired**, and the parts that are wired are tuned for a demo rather than a long
task. That is what this plan attacks.

---

## Phase 0 — Finish what is already half-built (2–3 days)

These are known defects or dead configuration. Highest value per hour, no design
work needed.

### 0.1 Kill the subprocess on verification timeout — **S**
`verification._run` wraps `communicate()` in `wait_for`, but on timeout it never
kills the child. The orchestrator moves on; the process keeps running.
*Done when:* a deliberately hanging test command leaves no orphan process.

### 0.2 Cache health checks — **S**
`router.select` calls `adapter.health_check()` for **every candidate on every
subtask**. For API providers that is a live HTTP request with your key, per
routing decision — latency, rate-limit burn, and a hard dependency on the network
just to choose an agent.
*Done when:* health is cached with a short TTL (~30 s) and a forced refresh on
failure; a 5-subtask task issues one health probe per provider, not five.

### 0.3 Stop `chat_stream` double-executing — **M**
When a stream yields nothing, the fallback calls `execute()` — but the default
`stream()` is itself implemented via `execute()`, so an empty-but-valid reply is
paid for twice.
*Done when:* the fallback only fires for a genuine stream *error*, with a test
that counts adapter invocations.

### 0.4 Open Code prompt exceeds the Windows command line — **S**
`OpencodeCliAdapter` passes the whole prompt as one argv element. Context
packages plus recent messages will pass the ~32 KB limit and fail opaquely.
*Done when:* long prompts go via stdin or a temp file, with a test at 64 KB.

### 0.5 Wire or delete the dead settings — **S**
Verified unused outside `settings.py`: `network_retries`,
`half_open_max_calls`. `tenacity` is a declared dependency that is never
imported. Either implement retry/backoff and half-open probe limiting, or remove
the knobs — configuration that does nothing is worse than none, because it
implies a guarantee.
*Done when:* every setting in `config/aether.default.yaml` has a test proving it
changes behaviour.

---

## Phase 1 — Make long tasks survivable (1 week)

Today's defaults are tuned for a task that finishes in seconds. These are the
things that break at hour three.

### 1.1 Context budgeting for agent prompts — **M** ⭐
`ContextBudget` exists and is good — and is only used by **chat**. Orchestrator
prompts are assembled in `ContextPackage.to_prompt_block()` with fixed slices
(last 20 decisions, 50 files) and no token accounting. A long task will silently
exceed the provider window and fail as `CONTEXT`, repeatedly.
*Done when:* the context package is budgeted against the target provider's
window, compacting decisions/files/handoff before send; a test drives a task with
500 files and 200 decisions without a CONTEXT failure.

### 1.2 Real token counting — **M**
Six places estimate tokens as `len(text) // 4`. Budgets, quota and compaction
thresholds all inherit that error, which is large for code and non-English.
*Done when:* a `TokenCounter` per provider family (`tiktoken` where applicable,
the chars/4 heuristic as the documented fallback), used everywhere.

### 1.3 Per-provider cost — **S**
`usd_per_1k_tokens` is one global number. A task mixing Opus and a local model
reports fiction.
*Done when:* input/output rates live in provider config; `aether metrics` breaks
cost down per provider.

### 1.4 Live agent output — **M**
`agent_stream` is emitted **once, after** `execute()` returns. For a ten-minute
agent the UI shows nothing, then everything.
*Done when:* the orchestrator consumes `adapter.stream()` for streaming-capable
providers and emits incrementally; the dashboard shows tokens as they arrive.

### 1.5 Retry with backoff before failover — **M**
A single transient network blip currently burns a failover and a circuit-breaker
failure. `network_retries` exists for exactly this and does nothing.
*Done when:* NETWORK/TIMEOUT retries in place with jittered backoff, and only
then failover; circuit breaker counts a *retry-exhausted* attempt, not each try.

---

## Phase 2 — Trust the results (1 week)

### 2.1 Sandbox verification — **L** ⭐
The verification gate runs `pytest` **on agent-written code in-process, with no
isolation**. That is arbitrary code execution as part of the "don't trust the
agent" step. The `ExecutionSandbox` gates *whether* it runs, not *what it can do*.
*Done when:* verification can run in a container (Docker/Podman) or a restricted
subprocess, opt-in via config, with the current behaviour as an explicitly
documented "trusted workspace" mode.

### 2.2 A planner worth the name — **L** ⭐
`WorkflowEngine._plan_solo` decides the shape of the work with
`if any(k in prompt for k in ("build", "implement", "code", ...))`. "Refactor the
auth module" contains none of those words and gets the generic path. This is the
single biggest quality ceiling in the product.
*Done when:* an LLM planner produces the subtask DAG against a JSON schema, with
the current keyword planner as a deterministic fallback when no provider is
available or the plan fails validation. The orchestrator still owns execution —
only the *plan* comes from a model.

### 2.3 Verification that matches the goal — **M**
Checks are fixed: files exist, Python compiles, pytest passes. A research task is
"verified" by finding a `.md` file.
*Done when:* verification strategies are selected per task type and the
acceptance criteria are recorded on the task, so "done" means something specific.

### 2.4 Strengthen the tautological tests — **S**
Several failure-scenario tests assert `status in {COMPLETED, FAILED}`, which
`wait_terminal` already guarantees — they pass whether or not failover works.
*Done when:* each asserts the actual mechanism (which provider ran, that a
failover event fired, that a checkpoint exists).

---

## Phase 3 — Concurrency that scales past one worktree (1–2 weeks)

### 3.1 Per-agent worktrees — **L**
Parallel agents share one git working tree; the post-execution phase is
serialised with a lock to keep it safe, which means parallel mode is parallel
only during model calls. `git worktree` per agent would make it genuinely
concurrent and remove the lock.
*Done when:* two agents can edit different files simultaneously; merge conflicts
still surface as a resolver subtask.

### 3.2 Postgres path — **M**
"Portable to Postgres" is claimed and never exercised. SQLite is a single writer;
the JSON state files are last-writer-wins under a file lock.
*Done when:* CI runs the suite against Postgres too, and control-plane state can
live in the DB instead of JSON.

---

## Phase 4 — Make it adoptable (3–4 days)

### 4.1 Ship it — **S**
Publish to PyPI so it is `pipx install aether-orchestrator`, not
`git clone` + `pip install -e`. Tag `v0.1.0`.

### 4.2 Dashboard beyond a status page — **M**
It lists tasks and streams events. Worth adding: the subtask DAG, artifact diffs,
and inline approve/deny with the diff visible.

### 4.3 Contribution basics — **S**
`CONTRIBUTING.md`, issue/PR templates, and a `good first issue` pass — Phase 0 is
full of them.

### 4.4 Reconcile the docs — **S**
`README` says "not a chatbot"; `docs/PRODUCT.md` says "AETHER is an LLM router
that works like an AI agent terminal". Both are half-true and readers deserve one
answer.

---

## Phase 5 — Optional, only if demand appears

Redis queue and multi-machine workers (interfaces already anticipate it) ·
provider capability auto-probing instead of hand-tuned scores · MCP tool support
so agents call tools through AETHER · scheduled/recurring tasks.

---

## Sequencing

| When | Focus | Outcome |
|------|-------|---------|
| Week 1 | Phase 0 | No known defects; every setting does something |
| Week 2 | Phase 1 | A multi-hour task completes without context or cost surprises |
| Week 3 | 2.1 + 2.4 | Untrusted code is contained; tests assert mechanisms |
| Week 4–5 | 2.2 | Planner stops being keyword matching |
| Later | 3, 4 | Real concurrency, then adoption |

**If only three things get done:** 1.1 (context budgeting), 2.1 (sandboxed
verification), 2.2 (real planner). The first stops long tasks failing, the second
closes the one genuinely dangerous hole, the third raises the quality ceiling.

---

## Deliberately not doing

- **Rewriting to a graph framework** (LangGraph et al). The orchestrator is the
  product; adopting someone else's control flow would invert the core principle.
- **Auto-approving destructive commands.** The approval gate is the point.
- **Scraping subscription auth into API calls.** Non-negotiable.
- **Chasing dev-only npm advisories with major upgrades** while the dashboard is
  built-and-served and the dev server is out of the normal path.
