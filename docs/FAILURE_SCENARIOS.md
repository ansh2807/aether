# AETHER Failure Scenarios & Expected Recovery

| # | Scenario | Detection | Recovery |
|---|----------|-----------|----------|
| 1 | Agent timeout | execution wall-clock exceeds config | cancel subprocess; checkpoint; failover |
| 2 | Agent crash | non-zero exit / process death | handoff PARTIAL; failover |
| 3 | CLI unavailable | `shutil.which` / health_check fails | mark provider UNAVAILABLE; route elsewhere |
| 4 | Authentication failure | stderr/auth patterns or 401 | circuit + AUTH error; exclude until re-auth |
| 5 | Rate limit | 429 / rate-limit text | cooldown in QuotaManager; failover |
| 6 | Daily quota exhaustion | usage >= limit or provider message | status QUOTA_EXHAUSTED; failover |
| 7 | Network failure | connection errors | retry with backoff; then failover |
| 8 | Context overflow | provider error / token estimate | compact memory; reduce package; retry or failover |
| 9 | Invalid agent response | schema validation fails | synthetic handoff; optionally re-ask or failover |
| 10 | Malformed handoff | HandoffReport validation fails | synthesize from memory + logs |
| 11 | Incorrect code | verification FAILED | assign debugger role; re-verify loop |
| 12 | Tests fail | verification test check | same as 11 |
| 13 | Two agents same file | git merge conflict | pause parallel branch; conflict subtask |
| 14 | Orchestrator restarts | process exit | load TaskStore; resume RUNNING/QUEUED |
| 15 | Machine restarts | cold start | same as 14 from SQLite + workspace git |
| 16 | Provider back online | health poll succeeds; circuit HALF_OPEN→CLOSED | rejoin candidate pool |
| 17 | Multiple agents fail | cascade failures | try all candidates; then WAITING/FAILED + notify |
| 18 | Cancel during execution | user cancel API/CLI | cancel active executions; status CANCELLED; checkpoint |
| 19 | Command blocked by policy | `ExecutionSandbox` denylist / approval level / workspace escape | `POLICY_DENIED` before spawn; no circuit or quota penalty; no failover (same command would be denied everywhere); subtask FAILED with reason on timeline |
| 20 | Subtask stranded `RUNNING` | runner died / interrupted pause-resume left it unowned | lifecycle reclaims it to `READY` (`reclaimed` timeline event) instead of spinning |
| 21 | Unsatisfiable dependency | nothing ready, nothing running, nothing failed | task FAILED with `Deadlocked; no runnable subtasks remain: [...]` rather than hanging |
| 22 | Resume while a runner is still finishing | `start_task` finds a live runner | its stop flag is cleared so it resumes in place; if it exits first it hands over to a fresh runner |

## Retry Policy Defaults

- Max agent retries per subtask: 3
- Network retry: 2 with exponential backoff
- Circuit open after: 5 failures in window
- Circuit half-open after: 60s cooldown
- Verification fix loop max: 3
