# AETHER Handoff Protocol

## Purpose

Formal transfer of work between agents (or agent → orchestrator) so the next participant continues from a checkpoint, not from zero.

## Required Fields

| Field | Type | Required |
|-------|------|----------|
| handoff_id | string | auto |
| task_id | string | yes |
| from_agent | string | yes |
| to_agent | string \| null | optional |
| status | COMPLETE \| PARTIAL \| FAILED \| BLOCKED | yes |
| requested | string | yes |
| completed | list[string] | yes |
| remaining | list[string] | yes |
| files_changed | list[string] | yes |
| decisions | list[string] | yes |
| tests_executed | list[string] | no |
| test_results | string | no |
| known_bugs | list[string] | yes |
| assumptions | list[string] | yes |
| recommended_next_action | string | yes |
| artifacts | list[string] | no |
| logs | list[string] | no |

## Status Semantics

- **COMPLETE** — assigned work finished; remaining may be empty or out-of-scope follow-ups
- **PARTIAL** — progress made; remaining must be non-empty
- **FAILED** — could not make progress; known_bugs must explain
- **BLOCKED** — waiting on human/external dependency

## Malformed Handoff Recovery

If adapter cannot parse a structured handoff:

1. Orchestrator creates synthetic PARTIAL handoff
2. `completed` = ["raw execution finished with unparseable output"]
3. `remaining` = previous next_action or original subtask
4. `known_bugs` includes `malformed_handoff`
5. Raw output stored as artifact for audit

## Context Package for Next Agent

```
{
  "goal": "...",
  "current_state": "...",
  "decisions": [...],
  "files": [...],
  "known_issues": [...],
  "last_handoff": { ... },
  "checkpoint_id": "...",
  "your_assignment": "...",
  "constraints": [...]
}
```

No full conversation dump. Only scoped memory + latest handoff + assignment.
