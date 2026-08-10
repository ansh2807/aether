# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

Hardening pass over the orchestrator, control-plane state, local API and CLI.

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
