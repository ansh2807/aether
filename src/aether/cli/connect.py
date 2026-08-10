"""Provider connect flows - browser OAuth / CLI login / API key pages.

Like official agents: open browser (or CLI login), then verify connection.
We do NOT hijack third-party OAuth client_ids. For Claude we launch
`claude auth login` which uses Anthropic's own OAuth (same experience as Claude Code).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import webbrowser
from dataclasses import dataclass
from typing import Any, Optional

from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from aether.config.session import catalog_by_id, env_path, set_env_key
from aether.cli.ui import console



# Official pages for "get a key" / account connect (browser)
CONNECT_URLS: dict[str, str] = {
    "openai": "https://platform.openai.com/api-keys",
    "grok": "https://console.x.ai/",
    "gemini": "https://aistudio.google.com/apikey",
    "kimi": "https://platform.moonshot.cn/console/api-keys",
    "claude_cli": "https://claude.ai/login",
    "codex_cli": "https://platform.openai.com/api-keys",
    "opencode_cli": "https://opencode.ai/",
}


@dataclass
class ConnectResult:
    provider_id: str
    ok: bool
    method: str  # cli_oauth | api_key | skipped | failed
    message: str = ""
    detail: dict[str, Any] | None = None


def open_browser(url: str) -> bool:
    try:
        return bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001
        return False


def _run_cmd(argv: list[str], timeout: int = 120) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out
    except FileNotFoundError:
        return 127, "command not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


def claude_auth_status() -> dict[str, Any]:
    """Parse `claude auth status` JSON if possible."""
    code, out = _run_cmd(["claude", "auth", "status"], timeout=30)
    if code != 0:
        # try via npm cmd on Windows
        code, out = _run_cmd(["claude.cmd", "auth", "status"], timeout=30)
    text = out.strip()
    # status often prints pure JSON
    try:
        # find first { ... }
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        pass
    return {"loggedIn": False, "raw": text, "exit_code": code}


def _claude_cli_name() -> Optional[str]:
    if shutil.which("claude"):
        return "claude"
    if shutil.which("claude.cmd"):
        return "claude.cmd"
    return None


def claude_login_browser(
    console_mode: bool = False,
    *,
    force: bool = False,
    interactive: bool = True,
) -> ConnectResult:
    """
    Launch Claude's official OAuth login (opens browser - same style as
    claude.com/cai/oauth/authorize used by Claude Code).
    """
    cli = _claude_cli_name()
    if not cli:
        return ConnectResult(
            "claude_cli",
            False,
            "failed",
            "Claude CLI not installed. Install: npm i -g @anthropic-ai/claude-code",
        )

    status = claude_auth_status()
    if status.get("loggedIn") and not force:
        email = status.get("email") or "logged in"
        sub = status.get("subscriptionType") or status.get("authMethod") or ""
        return ConnectResult(
            "claude_cli",
            True,
            "cli_oauth",
            f"Already signed in as {email}" + (f" ({sub})" if sub else ""),
            detail=status,
        )

    if interactive:
        console.print(
            Panel.fit(
                "[bold]Connect Claude[/]\n\n"
                "This opens the [cyan]official Claude login[/] in your browser\n"
                "(same OAuth flow as Claude Code - not a pasted key).\n\n"
                "1. Browser opens -> sign in at claude.ai\n"
                "2. Approve access\n"
                "3. Return here when done",
                border_style="cyan",
                title="Claude OAuth",
            )
        )
        if not Confirm.ask("Open Claude login now?", default=True):
            return ConnectResult("claude_cli", False, "skipped", "User skipped Claude login")

    # Prefer subscription login (claude.ai) - matches user's OAuth URL style
    argv = [cli, "auth", "login", "--console" if console_mode else "--claudeai"]

    if interactive:
        console.print("[dim]Running: claude auth login - complete sign-in in the browser...[/]")

    # Start login: opens browser OAuth. For API/dashboard use DETACHED so HTTP doesn't block forever.
    try:
        if interactive:
            proc = subprocess.run(argv, timeout=300)
            code = proc.returncode
        else:
            # Non-blocking launch for dashboard - user finishes OAuth in browser
            flags = 0
            if os.name == "nt":
                flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)  # type: ignore[attr-defined]
                flags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)  # type: ignore[attr-defined]
            subprocess.Popen(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=flags if os.name == "nt" else 0,
                start_new_session=(os.name != "nt"),
            )
            open_browser(CONNECT_URLS["claude_cli"])
            return ConnectResult(
                "claude_cli",
                True,
                "cli_oauth_started",
                "Browser OAuth started - finish sign-in, then click Refresh status",
                detail={"started": True},
            )
    except Exception as exc:  # noqa: BLE001
        open_browser(CONNECT_URLS["claude_cli"])
        return ConnectResult(
            "claude_cli",
            False,
            "failed",
            f"Could not launch CLI login ({exc}). Opened claude.ai - run: claude auth login",
        )

    status = claude_auth_status()
    if status.get("loggedIn") or code == 0:
        email = status.get("email") or "ok"
        return ConnectResult(
            "claude_cli",
            True,
            "cli_oauth",
            f"Connected as {email}",
            detail=status,
        )
    return ConnectResult(
        "claude_cli",
        False,
        "failed",
        "Login did not complete. Try: claude auth login",
        detail=status,
    )


def save_api_key(provider_id: str, api_key: str, data_dir) -> ConnectResult:
    """Save API key from dashboard (no interactive prompts)."""
    cat = catalog_by_id(provider_id)
    if not cat or cat.get("kind") != "api":
        return ConnectResult(provider_id, False, "failed", "Not an API provider")
    env_name = cat.get("api_key_env") or "OPENAI_API_KEY"
    key = (api_key or "").strip()
    if not key:
        return ConnectResult(provider_id, False, "failed", "Empty API key")
    set_env_key(env_path(data_dir), env_name, key)
    return ConnectResult(provider_id, True, "api_key", f"Saved {env_name}")


def provider_connect_info(provider_id: str) -> dict[str, Any]:
    """Describe how to connect a provider (for dashboard)."""
    cat = catalog_by_id(provider_id) or {}
    kind = cat.get("kind", "unknown")
    info: dict[str, Any] = {
        "provider_id": provider_id,
        "name": cat.get("name", provider_id),
        "kind": kind,
        "connect": cat.get("connect"),
        "api_key_env": cat.get("api_key_env"),
        "key_url": CONNECT_URLS.get(provider_id) or cat.get("key_url"),
        "has_key": bool(cat.get("api_key_env") and os.environ.get(str(cat.get("api_key_env")))),
        "cli_present": bool(shutil.which(cat.get("command", "") or "")),
    }
    if provider_id in ("claude_cli", "claude"):
        st = claude_auth_status()
        info["claude"] = {
            "loggedIn": bool(st.get("loggedIn")),
            "email": st.get("email"),
            "authMethod": st.get("authMethod"),
            "subscriptionType": st.get("subscriptionType"),
            "orgName": st.get("orgName"),
        }
        info["connected"] = bool(st.get("loggedIn"))
    elif provider_id == "opencode_cli":
        bin_path = shutil.which("opencode") or shutil.which("opencode.cmd")
        info["cli_present"] = bool(bin_path)
        if bin_path:
            code, listing = _run_cmd([bin_path, "auth", "list"], timeout=20)
            info["opencode_auth"] = listing[:400]
            info["connected"] = "-" in listing or "api" in listing.lower() or bool(bin_path)
        else:
            info["connected"] = False
    elif kind == "api":
        info["connected"] = info["has_key"]
    elif kind == "cli":
        info["connected"] = info["cli_present"]
    elif kind == "mock":
        info["connected"] = True
    else:
        info["connected"] = False
    return info


def connect_provider_api(
    provider_id: str,
    data_dir,
    *,
    force: bool = False,
    api_key: Optional[str] = None,
) -> ConnectResult:
    """Non-interactive connect for HTTP API / dashboard."""
    if provider_id in ("claude", "anthropic"):
        provider_id = "claude_cli"
    cat = catalog_by_id(provider_id)
    if not cat:
        return ConnectResult(provider_id, False, "failed", "Unknown provider")
    if cat.get("kind") == "mock":
        return ConnectResult(provider_id, True, "skipped", "Mock ready")
    if provider_id == "claude_cli":
        return claude_login_browser(force=force, interactive=False)
    if provider_id == "codex_cli":
        return codex_login()
    if provider_id == "opencode_cli":
        return opencode_login(interactive=False, force=force)
    if cat.get("kind") == "api":
        if api_key:
            return save_api_key(provider_id, api_key, data_dir)
        url = CONNECT_URLS.get(provider_id) or cat.get("key_url")
        if url:
            open_browser(str(url))
        env_name = cat.get("api_key_env")
        if env_name and os.environ.get(env_name):
            return ConnectResult(
                provider_id,
                True,
                "api_key",
                f"Key already set ({env_name}). Opened console if needed.",
            )
        return ConnectResult(
            provider_id,
            True,
            "browser_opened",
            f"Opened key page. Paste key via dashboard or POST /api/connect/{provider_id}/key",
            detail={"key_url": url, "api_key_env": env_name},
        )
    return ConnectResult(provider_id, False, "failed", "Unsupported provider")


def codex_login() -> ConnectResult:
    if not shutil.which("codex") and not shutil.which("codex.cmd"):
        return ConnectResult("codex_cli", False, "failed", "Codex CLI not found on PATH")
    # Best-effort - commands vary by version
    for argv in (
        ["codex", "login"],
        ["codex", "auth", "login"],
    ):
        code, out = _run_cmd(argv, timeout=60)
        if code == 0:
            return ConnectResult("codex_cli", True, "cli_oauth", "Codex login ok")
    open_browser(CONNECT_URLS["codex_cli"])
    console.print("[dim]Opened OpenAI platform. If Codex has its own login, run it in a terminal.[/]")
    return ConnectResult(
        "codex_cli",
        bool(shutil.which("codex")),
        "cli_oauth",
        "Codex present - complete any browser login if prompted",
    )


def opencode_login(*, interactive: bool = True, force: bool = False) -> ConnectResult:
    """Open Code multi-model auth: `opencode auth login`."""
    bin_path = shutil.which("opencode") or shutil.which("opencode.cmd")
    if not bin_path:
        return ConnectResult(
            "opencode_cli",
            False,
            "failed",
            "Open Code CLI not found. Install from https://opencode.ai or: npm i -g opencode-ai",
        )

    code, listing = _run_cmd([bin_path, "auth", "list"], timeout=30)
    has_creds = "-" in listing or "credential" in listing.lower() or "api" in listing.lower()
    if has_creds and not force:
        return ConnectResult(
            "opencode_cli",
            True,
            "cli_oauth",
            "Open Code credentials present (opencode auth list)",
            detail={"auth_list": listing[:500]},
        )

    if interactive:
        console.print(
            Panel.fit(
                "[bold]Connect Open Code[/]\n\n"
                "Open Code is a [cyan]multi-model hub[/] (Claude, Grok, Kimi, Google, GPT, ...).\n"
                "This runs [bold]opencode auth login[/] so you can sign into providers.\n\n"
                "After login you pick models inside AETHER.",
                border_style="magenta",
                title="Open Code",
            )
        )
        if not Confirm.ask("Start Open Code login?", default=True):
            return ConnectResult("opencode_cli", False, "skipped", "User skipped Open Code login")

    argv = [bin_path, "auth", "login"]
    try:
        if interactive:
            proc = subprocess.run(argv, timeout=300)
            ok = proc.returncode == 0
        else:
            flags = 0
            if os.name == "nt":
                flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
            subprocess.Popen(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=flags if os.name == "nt" else 0,
                start_new_session=(os.name != "nt"),
            )
            open_browser(CONNECT_URLS["opencode_cli"])
            return ConnectResult(
                "opencode_cli",
                True,
                "cli_oauth_started",
                "Open Code login started - finish in the terminal/browser, then Refresh",
            )
    except Exception as exc:  # noqa: BLE001
        return ConnectResult("opencode_cli", False, "failed", str(exc))

    code, listing = _run_cmd([bin_path, "auth", "list"], timeout=30)
    return ConnectResult(
        "opencode_cli",
        True,
        "cli_oauth",
        "Open Code login finished" if ok else "Login attempted - check opencode auth list",
        detail={"auth_list": listing[:500]},
    )


def connect_api_key_provider(
    provider_id: str,
    data_dir,
    open_page: bool = True,
) -> ConnectResult:
    """Open official key page (like OpenAI dashboard), then accept paste."""
    cat = catalog_by_id(provider_id)
    if not cat:
        return ConnectResult(provider_id, False, "failed", "Unknown provider")

    env_name = cat.get("api_key_env") or "OPENAI_API_KEY"
    name = cat.get("name", provider_id)
    url = CONNECT_URLS.get(provider_id) or cat.get("key_url")

    existing = os.environ.get(env_name, "")
    if existing:
        masked = existing[:4] + "..." + existing[-4:] if len(existing) > 10 else "****"
        console.print(f"[green]+[/] {name}: found {env_name}={masked}")
        if Confirm.ask("  Use existing key?", default=True):
            return ConnectResult(provider_id, True, "api_key", f"Using existing {env_name}")

    console.print(
        Panel.fit(
            f"[bold]Connect {name}[/]\n\n"
            f"Like the official app:\n"
            f"1. Browser opens the provider key / console page\n"
            f"2. Create or copy an API key\n"
            f"3. Paste it here (hidden)\n\n"
            f"Env var: [cyan]{env_name}[/]\n"
            f"Page: [link]{url or 'n/a'}[/link]",
            border_style="blue",
            title="API key connect",
        )
    )

    if open_page and url:
        if Confirm.ask("  Open browser to get a key?", default=True):
            if open_browser(url):
                console.print(f"  [dim]Opened {url}[/]")
            else:
                console.print(f"  Open manually: {url}")

    try:
        import getpass

        key = getpass.getpass("  Paste API key (hidden, Enter to skip): ").strip()
    except Exception:  # noqa: BLE001
        key = Prompt.ask("  Paste API key", password=True).strip()

    if not key:
        return ConnectResult(provider_id, False, "skipped", "No key entered")

    set_env_key(env_path(data_dir), env_name, key)
    console.print(f"  [green]+ saved to data/.env as {env_name}[/]")
    return ConnectResult(provider_id, True, "api_key", f"Saved {env_name}")


def connect_provider(provider_id: str, data_dir) -> ConnectResult:
    """Main entry: OAuth/CLI for Claude, key page for APIs."""
    cat = catalog_by_id(provider_id)
    if not cat:
        return ConnectResult(provider_id, False, "failed", "Unknown provider")

    kind = cat.get("kind")
    if kind == "mock":
        return ConnectResult(provider_id, True, "skipped", "Mock - no auth")

    if provider_id == "claude_cli":
        return claude_login_browser(console_mode=False)

    if provider_id == "codex_cli":
        return codex_login()

    if provider_id == "opencode_cli":
        return opencode_login(interactive=True)

    if kind == "cli":
        cmd = cat.get("command", "")
        path = shutil.which(cmd)
        if path:
            return ConnectResult(provider_id, True, "cli_oauth", f"CLI found at {path}")
        return ConnectResult(provider_id, False, "failed", f"CLI not found: {cmd}")

    if kind == "api":
        return connect_api_key_provider(provider_id, data_dir)

    return ConnectResult(provider_id, False, "failed", f"Unsupported kind {kind}")
