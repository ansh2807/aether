# Durability guarantees

AETHER's central claim is that **a task finishes even when the thing running it
dies**. This document says exactly what that covers, what it does not, and which
test proves each line.

The rule for this page: **no claim without a test behind it.** If a row has no
test id, it does not belong here. Run them yourself:

```bash
pytest tests/test_durability.py tests/test_retry.py tests/test_export_resume.py -q
```

---

## What survives

| Failure | Survives | What actually happens | Proof |
|---|---|---|---|
| Provider returns an error | ✅ | classified by `FailureClass`, checkpointed, routed to the next capable agent | `test_2_agent_crash`, `test_4_auth_failure`, `test_5_rate_limit`, `test_6_quota_exhaustion` |
| Provider hangs | ✅ | wall-clock timeout kills the subprocess, then failover | `test_1_agent_timeout`, `test_verification_run_kills_process_on_timeout` |
| Transient network blip | ✅ | retried on the same provider with full-jitter backoff *before* any failover; the circuit breaker sees one failure, not one per try | `test_two_network_failures_then_success_same_provider`, `test_timeout_is_retried_like_network`, `test_retries_exhausted_makes_exactly_retries_plus_one_attempts` |
| A failure retrying cannot fix | ✅ | `AUTH`, `QUOTA`, `POLICY_DENIED`, `CONTEXT`, `CANCELLED` fail over immediately instead of burning attempts | `test_auth_failure_returns_immediately_zero_retries`, `test_permanent_failure_classes_are_never_retried` |
| **Orchestrator is SIGKILLed mid-subtask** | ✅ | a fresh orchestrator over the same data dir requeues and finishes the task; no subtask runs twice; nothing is left stranded | `test_task_survives_sigkill_mid_subtask` (3 kill offsets), `test_recovery_is_recorded_not_silent` |
| **Agent output in flight when killed** | ⚠️ partial | CLI-adapter output is mirrored to disk as it arrives and salvaged into a `PARTIAL` handoff on the next attempt — **API and Open Code adapters do not yet capture** (see below) | `test_cli_adapter_writes_output_as_it_arrives`, `test_orchestrator_salvages_an_interrupted_run` |
| Subtask stranded `RUNNING` by a dead runner | ✅ | reclaimed to `READY` rather than spinning the lifecycle forever | `test_stranded_running_subtask_is_reclaimed` |
| A DAG that can never progress | ✅ | fails loudly with `Deadlocked; no runnable subtasks remain: [...]` instead of hanging | `test_unsatisfiable_dependency_fails_instead_of_hanging` |
| Long task outgrows the context window | ✅ | the package is budgeted against the *target provider's* window and compacted; assignment, goal and next action are never dropped | `test_package_fits_a_small_window`, `test_what_survives_compaction_is_what_the_agent_needs`, `test_long_task_runs_to_completion_without_a_context_failure` |
| **Moving a task to another machine** | ✅ | `export_bundle` carries the workspace; `import_bundle` unpacks it and rewrites `workspace_path` to the destination | `test_cross_machine_resume_round_trip` |
| Control-plane state across processes | ✅ | approvals, metrics, budgets and provider usage live in `data/state/` behind a cross-process file lock, so the CLI and the server see the same thing | `test_approval_visible_to_a_second_process`, `test_metrics_round_trip_between_processes`, `test_budget_round_trip_between_processes`, `test_quota_usage_survives_restart` |
| Two processes writing state at once | ✅ | locked read-modify-write; measured 120/120 increments preserved across 3 processes, versus 2/120 unlocked | `test_concurrent_processes_do_not_lose_updates` |
| Two agents writing the task at once | ✅ | locked read-modify-write on the task document; measured 10/10 concurrent completions preserved, versus 1/10 before | `test_concurrent_updates_do_not_lose_sibling_writes` |
| Cancelling mid-execution | ✅ | in-flight subprocesses are killed for every participating agent, not just the active one | `test_18_cancel_mid_flight`, `test_cancel_all_kills_every_in_flight_execution` |
| Two agents editing the same file | ✅ | the merge is a real merge; conflicts surface as a resolver subtask instead of one agent silently overwriting the other | `test_merge_detects_conflict_between_parallel_agents`, `test_merge_preserves_existing_main_commits` |
| A provider fails and the mock is available | ✅ | the mock is **not** routable while a real provider is enabled, so a fabricated success can never stand in for a real failure | `test_mock_not_routable_when_real_provider_configured` |
| Corrupt state file on disk | ✅ | treated as empty and rewritten rather than crashing the run | `test_store_survives_corrupt_file` |

---

## What is *not* guaranteed

Being specific here matters more than the table above. These are the ways you
can still lose work.

### In-flight output from API and Open Code providers

Only `CliAgentAdapter` mirrors output to the execution log. `ApiAgentAdapter` and
`OpencodeCliAdapter` still buffer their whole response in memory, so killing the
process mid-request loses everything received so far from those providers.

The salvage machinery is provider-agnostic — it reads whatever is on disk — so
this is a matter of wiring the remaining two adapters, tracked as the deferred
half of WO-02 and as WO-12 (live streaming) in [`ROADMAP.md`](ROADMAP.md).

### Machine reboot is inferred, not directly tested

`test_task_survives_sigkill_mid_subtask` kills the process with `SIGKILL` — no
cleanup, no atexit, no flush. A machine reboot exercises the same recovery path
(cold start over the same data dir), but nothing in the suite actually reboots a
machine, so treat that row as strongly implied rather than proven.

### One machine at a time

- **SQLite is a single writer.** Two orchestrators over one database is not
  supported and not tested. `WO-14` covers Postgres.
- **State files are last-writer-wins at file granularity.** The lock makes
  concurrent read-modify-write safe on one host; it says nothing about NFS or
  two machines sharing a directory.
- **The queue is in-process asyncio.** Nothing survives to another host on its
  own; moving work is the explicit export/import step above.

### Budgets and approvals during a hard kill

Budgets and metrics are flushed to `data/state/` as they change, so a kill loses
at most the last unflushed increment. An approval that a *blocking* wait was
parked on (`approval_timeout_seconds > 0`) does not survive the process — the
gate is persisted and re-resolvable, but the coroutine waiting on it is not.

### Prompt content outside the context budget

`sub.description` is appended to the prompt *after* the package is budgeted, so a
very large description — realistically, accumulated verification-failure detail —
can still push a request over the window. Known, unfixed.

### Verification runs untrusted code

The verification gate executes agent-written `pytest` in the workspace with no
isolation. That is a safety gap, not a durability one, but it is the most
dangerous known hole and is tracked as `WO-13`.

---

## How recovery actually works

1. **Checkpoint.** After every agent handoff, task memory plus an optional git
   commit are written. Provider death resumes from the last checkpoint, not from
   the start.
2. **Capture.** Supported adapters mirror output to
   `workspace/<task>/logs/exec-<subtask>.partial` as it arrives.
3. **Restart.** A fresh orchestrator calls `recover_running()`, which collects
   every non-terminal task *before* mutating any of them — requeuing moves a task
   between status buckets, and scanning-and-mutating in one pass re-processed the
   same task from a stale object.
4. **Salvage.** Before re-running a subtask, any leftover capture becomes a
   `PARTIAL` handoff, recorded on the timeline as `salvaged`.
5. **Reclaim.** A subtask left `RUNNING` with no live runner returns to `READY`.
6. **Continue.** The next agent receives a context package built from the
   surviving memory, budgeted to its own window.

Every step is visible on `aether task inspect TASK-...` — recovery is never
silent, because an operator who cannot see that a recovery happened cannot trust
that one did.

---

## Verifying your own install

```bash
aether doctor          # git, workspace writability, state dir, database
pytest tests/test_durability.py -q
```

`WO-09` adds `aether doctor --durability`, which will report which of the
guarantees above hold on *your* machine rather than in CI.
