# AETHER Implementation Roadmap

## Phase 1 — Architecture ✅
- Requirements analysis, contradictions, stack, diagrams, models, protocols

## Phase 2 — Provider abstraction
- Domain models, interfaces, config loading, adapter base classes

## Phase 3 — Task state engine
- TaskStore, lifecycle, memory, message bus, audit

## Phase 4 — CLI adapter + API adapter
- Generic CLI subprocess adapter, OpenAI-compatible API adapter, stubs, registry

## Phase 5 — Context + memory + handoff
- ContextManager, HandoffManager, CheckpointManager (git)

## Phase 6 — Routing + failover
- AgentRouter, QuotaManager, HealthManager, CircuitBreaker

## Phase 7 — Orchestrator + verification
- Planner, execution loop, VerificationEngine, WorkflowEngine (DAG)

## Phase 8 — Parallel + debate + collab modes
- Mode runners with budgets and max rounds

## Phase 9 — CLI + API
- Typer CLI, FastAPI REST + WebSocket

## Phase 10 — UI
- React dashboard: tasks, agents, timeline, health

## Phase 11 — Security
- Redaction, sandbox, approval gates, allowlists

## Phase 12 — Testing
- Unit + integration + 18 failure scenarios

## Phase 13 — Packaging + docs
- README, install, default config
