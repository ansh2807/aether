# AETHER Product Vision

## What AETHER is

AETHER has **two surfaces over one control plane**, and this document covers the
first of them:

- **The terminal** — an LLM router that behaves like an AI agent terminal
  (Grok / Claude Code style). Chat, debate, session memory, automatic model
  switching. That is the subject of everything below.
- **The orchestrator** — `aether run "goal"` plans a subtask DAG and gates it on
  verification. See the [README](../README.md) and
  [DURABILITY.md](DURABILITY.md).

They are not competing products. The terminal is the interactive way in; the
orchestrator is what runs when a goal is bigger than a conversation. Both sit on
the same routing, failover, persistence and budgeting machinery — which is why
switching a model mid-conversation and switching an agent mid-subtask are the
same code path.

It is **not** primarily a website research tool (WIOS can be added later as a module).

> **On "AETHER is not a chatbot."** Earlier drafts of the README said that while
> this document described a chat terminal. The README was overstating a real
> point. AETHER *does* have a chat surface; what it does not do is let the model
> own the session. Routing, failover, compaction, persistence and verification
> are decided by the orchestrator, not by whatever model happens to be answering.

### Core loop

```
You type in the terminal
        ↓
Active model answers
        ↓
┌───────────────────────────────────────┐
│ If rate limit / quota / crash / auth  │→ switch to next model
│ If context window nearly full         │→ summarize chat
│                                       │→ new session segment
│                                       │→ continue from summary
└───────────────────────────────────────┘
```

## Modes

| Mode | Command | Behavior |
|------|---------|----------|
| **Chat** (default) | just type | Single active model; auto-failover; auto-compact |
| **Debate** | `/debate topic` | Multiple models speak in rounds + consensus |
| **Task** | `/task goal` | Full orchestrator DAG (plan → implement → verify) |

## Features (implemented)

1. **Single-model chat** with provider pool  
2. **Automatic model switch** on RATE_LIMIT, QUOTA, AUTH, TIMEOUT, CRASH, CONTEXT  
3. **Debate mode** — structured multi-model dialogue (not uncontrolled chat)  
4. **Context compaction** — when usage ≥ ~78% of context window:  
   - summarize conversation (LLM or extractive fallback)  
   - close segment  
   - open new segment with summary as memory  
   - continue without restarting from zero  
5. **Session persistence** — `data/chat_sessions/`; `/resume`, `/sessions`, `/new`  
6. **Login lineup** — Claude · Google · Grok · Kimi · Open Code (multi-model)  
7. **Live token streaming** — terminal + dashboard SSE (`/api/chat/sessions/{id}/stream`)  
8. **Per-provider `context_window`** in config (YAML) drives compaction thresholds  
9. **Dashboard live chat** — same session engine as the terminal  

### Chat API

```
POST /api/chat/sessions
GET  /api/chat/sessions
GET  /api/chat/sessions/{id}
POST /api/chat/sessions/{id}/messages
POST /api/chat/sessions/{id}/stream   # SSE tokens
POST /api/chat/sessions/{id}/compact
POST /api/chat/debate
```

## Terminal

```bash
aether          # login + terminal
aether term
START_HERE.bat
```

```
❯ (mock) hello
❯ (mock) /debate Best stack for realtime file transfer
❯ (mock) /task Build a small Python CLI
❯ (mock) /compact
❯ (mock) /model mock_b
```

## Future modules

- **WIOS** (Website Intelligence OS) can sit on AETHER’s orchestrator later  
- Does not change the core product: **LLM router agent terminal**
