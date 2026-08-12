# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Security — production hardening

- Verification now defaults to container isolation and fails closed when the
  requested runtime is unavailable. Host subprocess fallback requires explicit
  opt-in, and verification rejects workspace symlinks.
- Verification containers now use an image built from pinned inputs, an
  unprivileged UID, zero Linux capabilities, no-new-privileges, disabled
  networking and IPC, bounded CPU/memory/swap/pids/files, and forced timeout
  cleanup. Registry-hosted custom images can be locked to an immutable digest.
- The supported local verifier image now carries exact, hash-checked pytest
  tooling, so the safe default can execute tests without network access.
- Dashboard bootstrap credentials now travel in a URL fragment, are exchanged
  for a signed HttpOnly SameSite cookie, and are removed from browser history.
  Ordinary HTTP query-token authentication is rejected; API docs are gated.
- API responses add CSP, anti-framing, no-referrer and no-store headers, and
  cookie-authenticated mutations enforce trusted origins.
- Control-plane JSON state no longer continues after lock timeouts, corruption,
  or failed atomic commits.
- Wheels now include the default configuration and built dashboard, with a clean
  install smoke test in CI. Vite was upgraded and `npm audit` is clean.
- The supported Compose profile binds to loopback, persists only intended data,
  uses a read-only root filesystem, drops capabilities, enables
  no-new-privileges, and applies conservative resource limits.
- Docker build stages and the default verification runtime are pinned by digest
  rather than following mutable upstream tags.
- The runtime image moved from Debian slim to Alpine, removing the Git/Perl
  package chain that the container vulnerability scan flagged.
- Live provider changes rebuild the active registry in place instead of leaking
  a second application instance behind stale FastAPI route closures.
- The mock provider is disabled by default. Simulation now requires the explicit
  `aether run --demo ...` flag and prints a warning that no real work is occurring.

Hardening pass over the orchestrator, control-plane state, local API and CLI.

### Added — cyber skill: reviewing the code the agents wrote

- **`aether.skills.cyber` — secure code review as a verification gate.**
  Verification proved artifacts existed and Python parsed; neither notices that
  `subprocess.run(cmd, shell=True)` compiles perfectly. "Verification before
  trust" was only asking whether the work happened, never whether it was safe.
- Fifteen detectors, each mapped to a CWE: secrets, shell/SQL/code injection,
  unsafe deserialisation, XSS sinks, XXE, disabled TLS verification, weak
  crypto, debug exposure. `HIGH` fails the task; the rest are reported.
- **Deterministic first, model second.** The regex pass is offline and
  reproducible and still runs when every provider is down. Only the judgement
  calls — is that string actually attacker-controlled? — go to a model, through
  the normal router, so it works with any provider. Silence from the model
  never clears a finding: an unreachable provider must not read as a clean
  bill of health.
- **Defensive only, and pinned by a test** that inspects the module's imports:
  no sockets, no HTTP clients, no remote scanning, no exploitation tooling.
  AETHER is pointed at people's own repositories.
- Structure borrowed from mukul975/Anthropic-Cybersecurity-Skills: `SKILL.md`
  with YAML frontmatter and framework mappings. The offensive half of that
  catalogue is deliberately not adopted.
- **Dogfooding found the worst flaw immediately.** Its first run on AETHER
  produced three findings, two of them in the scanner's own source — matching
  its module docstring's example of `shell=True` and one of its own rule
  regexes. Prose describing an insecure pattern is not an insecure pattern, and
  a scanner that blocks a build on its own documentation is one people switch
  off. Comments and docstrings are now skipped, the rules file excludes itself,
  and a test asserts the scanner stays clean against AETHER's own source.
- Suppressions apply to a whole comment block, not just the line. Found the
  same way: AETHER's `PRAGMA busy_timeout` suppression is two comment lines
  above the call, and both a same-line check and a one-line lookback ignored it.

### Fixed — the verifier image failure named the wrong problem

- Container mode with Docker present but `aether:local` unbuilt fell through to
  Docker's own "Unable to find image … pull access denied", which reads like a
  registry permissions problem and never mentions that the image is built
  locally with one command. Now detected separately, with the build command in
  the message.

### Added — modality routing: media work no longer goes to text models

- **The bug.** Every capability dimension was text-work, so "generate a
  10-second promo video" scored as *coding* and went to whichever model writes
  the best Python — which returned a confident paragraph describing a video.
  A plausible wrong answer is the worst failure this project recognises.
- **Modality is a gate, not a weight.** A subtask requiring `image`, `video` or
  `audio` may only route to a provider declaring it. Scoring it softly would let
  a text model win on capability and hand back prose. The mock is not exempt —
  fabricating an image is the same lie as fabricating a success.
- **A provider declares a modality only when AETHER can invoke it.** Gemini can
  generate video; AETHER cannot ask it to, so Gemini does not claim `video`. A
  test enforces this across the whole catalogue.
- **Image generation is real**, via `/images/generations` — the one media
  endpoint with a common shape across OpenAI-compatible servers. The image is
  written to the workspace as an artifact, so verification checks a file rather
  than a claim. `openai` and `omniroute` declare it.
- **Video and audio fail loudly** with a reason that names what is missing and
  what would otherwise go wrong, because "no available agents" reads as "try
  later" when the condition is permanent.
- Detection is deterministic and deliberately conservative: it needs a produce
  verb next to a media noun, and phrases like "fix the video player" or "add
  image upload" are explicitly excluded — those are coding tasks that merely
  contain the word.

### Added — per-agent memory across tasks

- **Specialists now remember their earlier work.** `TaskMemory` is scoped to one
  task and dies with it; this is keyed by *persona* and outlives the task, so
  the Database Optimizer does not rediscover on Monday that migrations run
  through `make migrate`.
- **Bounded on purpose.** Memory is injected into every prompt that persona
  receives, so an unbounded log would quietly eat the context window it was
  meant to improve. Capped per agent and per note; oldest drop first.
- **Failures are remembered alongside successes.** An agent that recalls only
  what worked repeats its mistakes with growing confidence.
- `aether agentic memory [slug] [--forget]` to inspect or clear it. Clearing
  matters: memory of a repo that has since been rewritten is misleading.
- Stored through `JsonStateStore`, so the CLI and a running server see the same
  notes. Writing a note is best-effort and can never fail a subtask that
  succeeded.

### Added — agentic view: a company of specialists over the model pool

- **`aether agentic org | who | plan | board`.** A roster of named specialist
  personas is staffed onto AETHER's providers. The three layers stay
  independent — role (what kind of work), persona (who does it), provider
  (which model runs them) — so a specialist survives their model rate-limiting
  and simply moves.
- **`plan` stops for approval.** It decomposes the goal, staffs every subtask,
  shows the reasoning per row, and runs nothing until you say yes.
- Staffing is a **deterministic keyword match, not an LLM call**: reproducible,
  explicable, and it still works when the planner's provider is down. It is
  dependable about the *division* and approximate about the *individual*, which
  is stated wherever it is shown.
- The persona's system prompt is **prepended to the subtask prompt**, so
  staffing changes what the model does rather than only what the table says —
  asserted by a test that captures the real request.
- Everything degrades to previous behaviour: no roster, a corrupt index, a
  deleted persona file or a roster that moves between planning and execution
  all fall back to "no persona", never to a failed task.
- Three staffing bugs were found and fixed by looking at real output rather
  than trusting the scores: the goal was weighted like the subtask (so every
  subtask of a "multiplayer game" goal matched on "game", sending marketing
  copy to a Game Audio Engineer); an unasked qualifier in a persona's name cost
  nothing (so "Healthcare Marketing Compliance Specialist" won landing-page
  copy on one word); and only the role could suggest a division (so marketing
  work had no marketing home). Each has a regression test.
- When the LLM planner is unavailable the CLI **says the plan is generic**
  rather than letting weak staffing look like a broken matcher.

### Added — free models are findable (`aether models --free`)

- **The catalogue had no notion of cost**, and OpenRouter's default model was
  `anthropic/claude-sonnet-4.5` — so anyone looking for something free could
  have picked it and been billed. Providers now declare `cost` as `free`
  (local hardware, never expires), `free_tier` (an allowance), `subscription`
  (a plan you already pay for) or `paid`.
- **`--free` names the model that is actually free**, not the default. For
  OpenRouter that is `deepseek/deepseek-r1:free`; a test asserts it never
  resolves to a billed one.
- **GLM (Z.AI / Zhipu) and Cerebras added.** GLM-4.5-Flash is the strongest
  genuinely-free hosted model in the catalogue.
- `subscription` CLIs are deliberately **excluded** from `--free`. A Claude plan
  has no per-token cost, but someone asking for free models is not asking about
  a paid subscription.
- The caveat is printed with the results: free tiers move constantly, these come
  from vendor docs rather than a live check, and `free` (your hardware) is a
  different promise from `free tier` (an allowance that can end).

### Added — closing the gap with a gateway

Taken from the OmniRoute comparison, now that the differences had been written
down honestly. Three of its advantages were real and worth having; the rest are
declined with reasons in `docs/ROADMAP.md`.

- **`/v1/chat/completions` and `/v1/models`** — an OpenAI-compatible endpoint on
  `aether serve`. Point Cursor, Cline, Aider, Continue or the `openai` SDK at
  `http://127.0.0.1:7420/v1` and they get AETHER's failover, compaction and
  budget gates without knowing AETHER exists. This was the one thing a gateway
  could do that AETHER could not: it is an endpoint, so existing tools work
  unchanged. Stateless, because the protocol is.
- **`/v1/` is token-gated.** The auth middleware guarded only `/api/`; a new
  unauthenticated route that spends money would have been an open relay to
  every provider key on the machine, reachable by any local process. Reverting
  the guard fails three tests, and CI asserts it against the running container.
- The response's `model` field and `X-Aether-Provider` report **who actually
  answered**, so a failover is visible rather than hidden. `X-Aether-Failovers`
  counts them. An unknown model is a 404, never a silent substitution.
- `stream=true` returns an explicit 400 rather than a malformed SSE body that
  would hang the client. Tracked as WO-26.
- **`omniroute` added to the provider catalogue.** Running it locally gives
  AETHER ~290 providers and their compression through one entry — breadth
  without adopting a provider list whose terms are not ours to restate.
- **Dockerfile + a CI job that boots the image**, hits `/api/health`, and
  asserts both `/api/` and `/v1/` reject unauthenticated calls. Building proves
  it compiles; only running proves it runs. Non-root, since verification
  executes agent-written code.
- **A weekly mutation workflow** (`mutmut`) over the routing core, filing an
  issue when mutants survive. CONTRIBUTING.md already required that a test be
  able to fail; this checks it rather than trusting memory. Hand-run mutations
  during this work caught three tests of mine that could not fail.

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
