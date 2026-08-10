# How to run AETHER (Windows)

## Turn it on (recommended)

**Double-click:** `START_HERE.bat`

Or:

```powershell
cd C:\Users\anshk\Desktop\aether
.\run.bat
```

AETHER will ask you, in order:

1. **Solo or Multi-agent** (also Debate / Parallel)
2. **Which models** (OpenAI, Grok, Gemini, Claude, Codex, Local, Mock)
3. **Connect accounts** (like official apps):
   - **Claude** → browser OAuth via `claude auth login` (same flow as Claude Code / claude.com OAuth)
   - **OpenAI / Grok / Gemini** → opens their key console in the browser → you paste the key
4. **Health check** each chosen model
5. Then you type your task

Also anytime:

```powershell
.\.venv\Scripts\aether.exe connect claude_cli
.\.venv\Scripts\aether.exe connect openai
```

Keys are **not** stored in task memory (`data/.env` only). Preferences: `config/aether.yaml` + `data/user_profile.yaml`.

---

## Why `aether` “doesn’t run”

Typing `aether` in a normal terminal fails unless the **virtualenv is activated**.

`aether` is installed at:

```text
C:\Users\anshk\Desktop\aether\.venv\Scripts\aether.exe
```

It is **not** on your global PATH by default.

---

## Easiest: double-click / batch files

Open a terminal **in the project folder**, or double-click:

| File | What it does |
|------|----------------|
| `START_HERE.bat` | Full turn-on wizard + chat |
| `run.bat` | Same; or pass commands e.g. `run.bat doctor` |
| `serve.bat` | Starts the API server on http://127.0.0.1:7420 |

### Examples

```bat
cd C:\Users\anshk\Desktop\aether

run.bat doctor
run.bat run "Build a small Python hello module"
run.bat status
run.bat task list
run.bat serve
```

---

## Correct PowerShell sequence

```powershell
cd C:\Users\anshk\Desktop\aether
.\.venv\Scripts\Activate.ps1
aether doctor
aether run "Build a small Python hello module"
```

If activation is blocked by execution policy:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Or **skip activation** and call the full path:

```powershell
cd C:\Users\anshk\Desktop\aether
.\.venv\Scripts\aether.exe doctor
.\.venv\Scripts\aether.exe run "Build a small Python hello module"
.\.venv\Scripts\aether.exe serve
```

---

## Start the API (dashboard backend)

```powershell
cd C:\Users\anshk\Desktop\aether
.\.venv\Scripts\aether.exe serve
```

Open: http://127.0.0.1:7420/api/health  
You should see: `{"status":"ok","version":"0.1.0"}`

### Optional web UI

Build it once; `aether serve` then hosts it at the same address as the API:

```powershell
cd C:\Users\anshk\Desktop\aether\web
npm install
npm run build
```

Then open the dashboard link printed by `aether serve` (it carries the token).
No second terminal, no dev server, no proxy.

`npm run dev` is only for live-editing the UI. Every open npm advisory in the
front-end tree is dev-server-only, so prefer the build above for normal use.

---

## Verified working command

This completed successfully on this machine:

```powershell
cd C:\Users\anshk\Desktop\aether
.\.venv\Scripts\aether.exe run "Build a tiny hello script"
```

Result: `status=COMPLETED` with files under `workspace\TASK-...`

---

## Common mistakes

| You type | Problem | Fix |
|----------|---------|-----|
| `aether` (from anywhere) | Not on PATH | `cd` to project + activate venv, or use `run.bat` |
| `aether` without activate | Command not found | `.\.venv\Scripts\Activate.ps1` first |
| Dashboard only | No backend | Run `serve.bat` / `aether serve` first |
| Expecting Claude/GPT without config | Only **mock** is enabled | Enable providers in config + API keys/CLIs |
| `UnicodeEncodeError: 'charmap' codec…` | Console is on a legacy code page | Fixed automatically now; the `.bat` files also set `chcp 65001`. If you launch `aether.exe` by hand from an old console, run `chcp 65001` first |
| Dashboard shows 401 / won't load | API is token-gated | Use the dashboard link printed by `aether serve` (it carries `?token=…`) |

Any command that fails now prints a one-line reason instead of a traceback.
Set `AETHER_DEBUG=1` to see the full traceback.

---

## Still stuck?

Copy the **exact error text** from your terminal (e.g. “not recognized”, traceback, hang).  
That message is required to fix the next issue.
