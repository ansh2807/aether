# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

Hardening pass over the orchestrator, control-plane state, local API and CLI.

### Fixed — routing decisions were unexplainable after the fact

- **The router's rationale was computed and thrown away.** `RouteDecision`
  carried `score`, `reasons` and `alternatives`; the `assigned` timeline event
  recorded only "subtask → provider (role=X)". Nothing outside the tests ever
  read the rest, so "why that provider, and what else was available?" could not
  be answered after a run. Exactly the dead-computation pattern WO-03 called
  out: it implies a guarantee it does not provide.
- Found by re-checking a claim rather than trusting it. The WO-24 entry above
  asserted routing stayed auditable because the load term was added to
  `reasons` — into a field with no production reader. The claim is struck
  through above rather than quietly deleted.
- The timeline now reads:
  `Plan approach → mock_a (role=architect) score=0.80 [capability:architecture=9, availability=AVAILABLE] over mock_b, mock_c`,
  and the WebSocket `agent_assigned` event carries score, reasons and
  alternatives for the dashboard.

### Fixed — Open Code lost in-flight output too (WO-02 complete)

- **The last adapter still calling `proc.communicate()` now streams.** It
  returned nothing until the child exited, so a kill at minute nine of a
  ten-minute run threw away all nine. Reuses the same `stream_process` helper
  the CLI adapter has used since WO-02; Open Code attaches stdin as a real file
  rather than a pipe, so there is nothing to feed concurrently.
- A timeout now leaves the partial output on disk instead of discarding it —
  a hung agent is the common case and the one where salvage pays off.
- Only stdout is mirrored. Open Code writes progress chatter to stderr, and
  folding it in would corrupt a handoff parsed from a salvaged run.
- `docs/DURABILITY.md`: this row moves to ✅, with the one remaining condition
  stated in the cell — API providers need SSE, and fall back to a buffered
  request where the server cannot stream.

### Fixed — tests

- **A test in this batch drove the real `opencode` CLI.** The adapter resolves
  its binary with `shutil.which("opencode")` and ignores `config.command`, so
  pointing the config at a script did nothing and six tests made live calls
  against the logged-in account. Now a wrapper binary on disk plus a patched
  `opencode_bin`. Tests must never reach a real provider.
- Those two streaming tests took 60s: the wrapper leaves a grandchild holding
  the pipe after the kill, so the child's own sleep set the pace. Shortened to
  5s — 11s now, and reverting the fix still fails them.
- `_FakeProc` in the WO-06 tests grew `stdout`/`stderr` readers. It only
  implemented `communicate()`, which no longer exists on this path.

### Fixed — API providers lost in-flight output when killed

- **The API adapter now streams and mirrors to disk as output arrives.** It
  issued one buffered POST and held the whole response in memory, so a kill at
  minute nine of a ten-minute generation lost all of it. The CLI adapter has
  streamed since WO-02; this was the other half, and it became the larger half
  once the catalogue filled with hosted APIs.
- Falls back to the buffered request when a server cannot stream, so SSE support
  is an optimisation rather than a requirement. On that path there is nothing to
  salvage — `docs/DURABILITY.md` says so rather than implying a guarantee that
  depends on the provider.
- A 401 or 429 is classified identically on both paths and is **not** re-sent as
  a fallback: the answer is the same either way, and WO-23 now treats `AUTH` as
  terminal, so getting the classification right matters more than it used to.
- Verified by mutation: reverting to buffered, or buffering the chunks and
  writing once at the end, both fail the tests. The second only fails because
  the test samples the log *during* the stream — asserting after it completes
  would have passed against the old adapter too.
- `docs/DURABILITY.md` moves this row from "partial" to "mostly"; Open Code is
  now the only adapter that still buffers.

### Added — routing hardening (WO-23, WO-24)

- **A revoked API key is no longer retried forever.** `record_failure` took only
  a provider id, so the breaker could not tell an expired credential from a
  network blip — it opened for 60s, half-opened, spent a real subtask attempt on
  a request that could not succeed, and repeated indefinitely. New `DISABLED`
  circuit state for `AUTH` and `POLICY_DENIED`: one attempt, then out of
  rotation, with a reason the operator can act on. `RATE_LIMIT` and `QUOTA` are
  deliberately *not* terminal — they recover on a clock, which is what `OPEN`
  is for.
- **Concurrent subtasks now spread across providers.** Selection ignored work in
  flight, so with `max_concurrent_subtasks: 4` all four dispatched subtasks
  scored identically and stacked on one provider while healthy ones idled. The
  router now keeps a per-provider in-flight count and applies a bounded load
  penalty. A single subtask still picks the best provider — asserted, so spread
  was not traded for quality.
- ~~The load term appears in the decision's `reasons`, keeping routing
  auditable.~~ **This was wrong when written** — see below. `reasons` was
  computed on every selection and read by nothing outside the tests.
- Scope, stated plainly: breaker state is in-memory and never persisted, so
  `DISABLED` lasts for the life of the process and a restart clears it. The
  planned `aether providers reset <id>` was **dropped** — a separate CLI process
  has its own empty breaker, so the command would have silently done nothing.
  `aether providers` now shows the disabled reason instead.

### Fixed — intermittent `database is locked`

- **SQLite ran with `journal_mode=delete` and `busy_timeout=5000`** (measured,
  not assumed). Under a rollback journal a writer takes an exclusive lock at
  commit and blocks readers; five seconds of patience is not much on a loaded
  two-core CI runner. This failed two CI runs in two unrelated tests, both on a
  read racing an orchestrator write. Now `journal_mode=WAL` and a 30s busy
  timeout, applied via a connect event guarded by dialect name so Postgres
  never sees it.
- `synchronous` is deliberately left at **FULL**. NORMAL is the usual companion
  to WAL and is faster, but trades durability on OS crash or power loss, and
  not losing work is the project's central promise. WAL alone fixes the
  contention.
- Side effect: the test suite got faster, ~85s to ~67s.
- The new tests are explicit about which of them can actually catch a
  regression. Restoring the old settings failed **only** the pragma assertion;
  the concurrency tests passed against the broken configuration too and say so
  in their docstrings rather than implying proof they do not provide.

### Added — contributor on-ramp and a runnable demo

- **`CONTRIBUTING.md`**, issue templates (bug / feature / config) and a PR
  template. The norm they lead with is *a test must be able to fail*: break your
  own fix on purpose and watch the new test go red. The PR template asks you to
  tick that you did, and to say so honestly if you did not.
- **`scripts/demo_failover.py`** — kills a task mid-flight and resumes it in a
  fresh process, no API keys required. It waits for a genuinely mid-flight
  moment (some subtasks done, some not) and **refuses to print `PASS` if it
  cannot catch one**, because the first version killed a task that had already
  finished and reported success having demonstrated nothing.
- The demo reports honestly that recovery may land on the *same* provider it
  died on. Routing scores capability, health and quota, and a killed process is
  not evidence against a provider; two are configured so it *can* move, not to
  promise it will.

### Changed — docs

- **README and `docs/PRODUCT.md` no longer contradict each other.** The README
  claimed AETHER is "not a chatbot" while PRODUCT.md described a chat terminal.
  Both now describe two surfaces over one control plane: the interactive
  terminal and the orchestrator. The accurate claim was always narrower — the
  model is never the thing in charge.
- Stale test-count badge corrected (176 → 223).

### Fixed — tests

- `test_orchestrator_call_site_falls_back_when_no_real_provider` polled just 5s
  for planning to complete and went flaky under a loaded full-suite run on
  Windows. Raised to 30s with a diagnostic message.
- Removed an `importlib.reload` from the catalogue tests. Reloading rebinds
  `session.PROVIDER_CATALOG` to a fresh list while every `from … import` holds
  the old one — a state no real run can reach, leaked into every later test.

### Added — provider catalogue

- **`aether models` / `/models`** — lists every provider AETHER can connect to,
  grouped as agent CLIs, aggregators, hosted APIs and local runtimes, with a
  readiness column for this machine. Boots nothing and reads no database, so it
  works before `aether setup`. Filters: `--ready`, `--kind`, `--search`.
- **~17 new providers** in `src/aether/config/providers.py` and the default
  config, all disabled: Anthropic, DeepSeek, Mistral, Groq, Together,
  Fireworks, Perplexity, Qwen, OpenRouter (aggregator); Gemini CLI, Aider,
  Cursor, Copilot and Grok CLIs; Ollama, LM Studio and vLLM locally. No new
  adapter code — the OpenAI-compatible and CLI adapters already cover all of
  them, so these are configuration.
- Readiness is checked honestly per group: CLIs against `PATH`, hosted APIs
  against the key variable, local runtimes with a 150 ms TCP probe. An earlier
  draft showed every local runtime as ready whether or not a server was
  running; a green tick that is always green means nothing.
- The catalogue states, in the CLI and the README, that most entries were
  transcribed from vendor documentation and never exercised from AETHER. The
  `verified` flag is asserted in tests to stay confined to what really has been.

### Changed

- **`/models` is no longer an alias for `/setup`.** It now shows the catalogue,
  which is what the name promises; `/setup` keeps `/reconfigure`.
- The login wizard still offers the short curated lineup. Extended providers are
  reachable via `aether connect <id>` and `aether models` but do not turn first
  run into a wall of thirty options — enforced by a test.
- Distribution renamed to **`aether-orchestrator`** (`aether` is taken on PyPI).
  The import package and the `aether` command are unchanged. Added classifiers,
  keywords and project URLs; the build passes `twine check`.
- README: corrected a stale claim that `kimi` ships as `adapter: unavailable` —
  it has been a real `api` provider for some time.

### Fixed — correctness

- **Mock agent could absorb real work.** `for_testing` providers were routable
  on failover, so a real provider hitting AUTH/RATE_LIMIT/CRASH could hand the
  subtask to the mock, which always reports `STATUS: COMPLETE` and writes a
  placeholder file — verification then passed. Testing providers are now
  routable only when no real provider is enabled
  (`routing.allow_testing_fallback` opts back in). Applies to chat failover too.
- **Agent branch merges never merged.** `merge_to_main` used
  `git checkout -B main`, which *resets* the base branch to the agent branch
  tip; the following merge was always "already up to date", so conflicts were
  never detected and concurrent agents silently overwrote main.
- **Parallel subtasks overwrote each other.** Each `_run_subtask` loaded and
  rewrote the whole task document. Measured: 1 of 10 concurrent completions
  survived. All task writes now go through a locked read-modify-write
  (`TaskManager.update`); the post-execution phase (memory, git worktree,
  checkpoint) is serialised per task.
- **Cancel left subprocesses running.** The orchestrator called
  `adapter.cancel("all")`, an id that never exists. Added `cancel_all()` to the
  adapter protocol; cancellation now covers every participating agent.
- **Stranded subtasks hung the lifecycle.** A subtask left `RUNNING` by a dead
  runner was neither "ready" nor "finished", so the loop spun forever. Such
  subtasks are now reclaimed, and a genuinely unsatisfiable DAG fails with
  `Deadlocked; no runnable subtasks remain: [...]`.
- **Pause/resume could orphan a task.** Resuming while a runner was still alive
  left its stop flag set (it paused straight back), and a runner exiting just
  after the liveness check left the task non-terminal with nobody driving it.
- **Daily quota never reset.** `daily_requests` only incremented, so a provider
  hit its cap once and stayed `QUOTA_EXHAUSTED` for the life of the process.
  Counters now carry a UTC day stamp and roll over at midnight.
- **Top-level `--config` was ignored by subcommands.** `aether --config X status`
  silently used the default data dir. A per-command `-c` still wins.
- Dashboard: `justifyBottom` typo meant the separator under each pending
  approval never rendered.

### Fixed — security

- **`ExecutionSandbox` was never called.** The command denylist, allowlist and
  `auto`/`safe`/`approval` levels were documented and configured but not wired
  into any execution path. Policy is now enforced before every subprocess AETHER
  starts (CLI adapter, Open Code adapter, verification), including workspace
  containment. Denial yields `FailureClass.POLICY_DENIED`, which does not
  penalise the provider or fail over. Policy is evaluated against the command
  AETHER chose to run, never the prompt.
- **Local API was unauthenticated with wildcard CORS.** `allow_origins + ["*"]`
  plus credentials let any page in the user's browser drive an API that creates
  tasks, spawns subprocesses and stores provider keys. Wildcard removed; the API
  is token-gated by default (`Authorization: Bearer`, `X-Aether-Token`, or
  `?token=` for WebSocket/SSE). Only `/api/health` is public.
- Sandbox: the network-command gate matched `"Invoke-WebRequest"` against an
  already-lowercased string and so could never fire.
- `aether serve` now hosts the built dashboard from `web/dist`, so the normal
  workflow needs no Vite dev server — where every current npm advisory in the
  front-end tree lives (path traversal in optimized-deps, `server.fs.deny`
  bypass on Windows, `launch-editor` NTLM disclosure). None are reachable if
  `npm run dev` is never started.

### Fixed — CLI

- **`UnicodeEncodeError` crashed commands on Windows.** Rich's legacy-console
  writer encodes with the active code page (cp1252 here), so a single `●` took
  `aether agents health` down. The console is switched to UTF-8 on startup,
  streams use `errors="replace"`, and glyphs fall back to ASCII when UTF-8 is
  not renderable. `.bat` launchers also set `chcp 65001`.
- Command failures print a one-line reason instead of a traceback
  (`AETHER_DEBUG=1` restores the traceback).

### Added

- Control-plane state persisted under `data/state/` (approvals, metrics,
  budgets, provider usage). Previously in-memory only, so `aether approvals`,
  `aether metrics` and `aether task trace` could never see a run from the server
  process, and a `WAITING` task became unresolvable after restart.
- `JsonStateStore` read-modify-write is guarded by an exclusive cross-process
  file lock (re-entrant per instance, `msvcrt`/`fcntl`). Without it, 3 processes
  × 40 increments landed 2 of 120; with it, 120 of 120.
- CLI presentation pass: shared theme, live task progress replacing per-poll
  line spam, width-adaptive tables, status/health colouring.
- `server.static_dir`, `server.require_auth`, `server.api_token`,
  `routing.allow_testing_fallback` settings.

### Changed

- FastAPI startup/shutdown moved to a `lifespan` handler (silences the
  `on_event` deprecation warnings).
- Verification `pytest` runs are policy-gated and recorded as `SKIPPED` with a
  reason when blocked, rather than executing unconditionally.

### Notes

Tests: 54 → 99. Several failure-scenario tests asserted
`status in {COMPLETED, FAILED}`, which `wait_terminal` already guaranteed — new
tests assert the actual behaviour and were each checked to fail against the old
code.

Known open items: routing calls `health_check()` per candidate per subtask (a
live HTTP request for API providers, uncached); `verification._run` does not
kill the child on timeout; Open Code passes the whole prompt as one argv element
(Windows command-line limit); `chat_stream` can double-execute when a stream
yields nothing.
