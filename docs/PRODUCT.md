# AETHER Product Vision

## What AETHER is

**AETHER is an LLM router that works like an AI agent terminal** (Grok / Claude Code style).

It is **not** primarily a website research tool (WIOS can be added later as a module).

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
