# AETHER Extensions (v0.1+)

This document describes capabilities added after the initial orchestrator core.

## Shared control-plane state

The CLI, the API server and any scheduled run are separate processes over one
`data_dir`. State a user can inspect or act on from either side is therefore
persisted as atomic JSON under `data/state/`:

| File | Holds |
|------|-------|
| `approvals.json` | pending + resolved approval gates |
| `metrics.json` | counters, per-provider latency/success, per-task traces |
| `budgets.json` | per-task token/request/cost budgets |
| `usage.json` | provider request counts (daily, UTC-rolling) and cooldowns |

Writers reload immediately before mutating, so concurrent processes accumulate
rather than reset each other. Writes are last-writer-wins at file granularity,
which suits a local single-operator tool.

## Human approval queue

- `ApprovalManager` holds pending gates (`PENDING` → `GRANTED`/`DENIED`/`EXPIRED`)
- Tasks can enter `WAITING` when `require_approval_on_start` or `approval_level=approval`
- Gates are persisted, so one process can raise and another can resolve
- Resolve via:
  - CLI: `aether approvals grant apr-...`
  - API: `POST /api/approvals/{id}/resolve`
  - Dashboard: Grant / Deny buttons

## Budgets

Per-task caps on tokens, requests, and optional USD estimate.

```python
await orchestrator.submit("...", max_tokens=50_000, max_requests=20, max_cost_usd=1.0)
```

Config defaults under `budget:` in YAML. Exhaustion fails the task cleanly with audit trail.

## Event bus + live stream

`EventBus` keeps a ring buffer and fans out to WebSocket clients.

Notable events: `task_created`, `agent_assigned`, `agent_started`, `agent_stream`,
`agent_finished`, `failover`, `approval_required`, `merge_conflict`, `task_completed`.

## Metrics & traces

`MetricsCollector` tracks task success rates, failovers, verification outcomes,
and per-provider latency / success rate.

- CLI: `aether metrics` / `aether task trace TASK-...`
- API: `/api/metrics`, `/api/metrics/tasks/{id}`

## Git agent branches

For `parallel` / `collab` / `debate` modes (when enabled):

1. Checkout `aether/{provider}-{subtask}`
2. Agent works
3. Commit
4. Merge to the base branch (`main`/`master`) with `--no-ff`
5. On conflict → abort the merge and spawn a **Resolve merge conflicts** debugger subtask

Agents in the same task share one worktree, so the post-execution phase (commit,
merge, memory and checkpoint writes) is serialised per task even when the agent
calls themselves run concurrently. Task-document writes go through a locked
read-modify-write (`TaskManager.update`) so parallel subtasks cannot overwrite
each other's status.

## Export / import

```bash
aether task export TASK-... -o backup.json
aether task import backup.json --resume
```

Also: `GET /api/tasks/{id}/export`, `POST /api/tasks/import`

## Doctor

```bash
aether doctor
```

Checks Python, git, DB, providers, API key env presence, CLI binaries, write access.

## Plugin adapters

### Config path

```yaml
providers:
  my_agent:
    display_name: My Agent
    adapter: api   # or cli
    enabled: true
    adapter_class: mypackage.adapters:MyAgentAdapter
```

### Entry points

```toml
[project.entry-points."aether.adapters"]
my_agent = "mypackage.adapters:MyAgentAdapter"
```

Implement the `AgentAdapter` protocol (see `interfaces/adapters.py`).
