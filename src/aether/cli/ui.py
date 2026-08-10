"""Shared console, theme and glyph set for the CLI.

Windows consoles default to a legacy code page (cp1252 here). Rich's legacy
console writer encodes with that code page, so a single `●` in a table raised
`UnicodeEncodeError` and took the whole command down. Two defences:

1. `init_console()` switches the console to UTF-8 and reconfigures the streams
   with ``errors="replace"``, so no glyph can ever crash a command again.
2. If UTF-8 still is not renderable (redirected output, exotic terminal), the
   decorative glyphs fall back to ASCII rather than turning into mojibake.

Everything user-facing should import `console` and `SYM` from here so the CLI
looks the same everywhere and stays safe on every terminal.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

AETHER_THEME = Theme(
    {
        "heading": "bold cyan",
        "accent": "cyan",
        "muted": "dim",
        "ok": "green",
        "warn": "yellow",
        "err": "red",
        "running": "cyan",
        "idle": "dim",
        "value": "bold",
        "rule.line": "dim cyan",
        "table.header": "bold",
    }
)

_UNICODE_SYMBOLS = {
    "online": "●",
    "offline": "○",
    "ok": "✓",
    "fail": "✗",
    "warn": "!",
    "arrow": "→",
    "bullet": "•",
    "dot": "·",
    "ellipsis": "…",
    "prompt": "❯",
    "dash": "—",
    "gte": "≥",
}

_ASCII_SYMBOLS = {
    "online": "*",
    "offline": "o",
    "ok": "+",
    "fail": "x",
    "warn": "!",
    "arrow": "->",
    "bullet": "-",
    "dot": ".",
    "ellipsis": "...",
    "prompt": ">",
    "dash": "-",
    "gte": ">=",
}

BANNER_UNICODE = r"""
    █████╗ ███████╗████████╗██╗  ██╗███████╗██████╗
   ██╔══██╗██╔════╝╚══██╔══╝██║  ██║██╔════╝██╔══██╗
   ███████║█████╗     ██║   ███████║█████╗  ██████╔╝
   ██╔══██║██╔══╝     ██║   ██╔══██║██╔══╝  ██╔══██╗
   ██║  ██║███████╗   ██║   ██║  ██║███████╗██║  ██║
   ╚═╝  ╚═╝╚══════╝   ╚═╝   ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝
"""

BANNER_ASCII = r"""
    /\   ___ _____ _  _ ___ ___
   /  \ | __|_   _| || | __| _ \
  / /\ \| _|  | | | __ | _||   /
 /_/  \_\___| |_| |_||_|___|_|_\
"""


def _enable_windows_utf8() -> bool:
    """Put the Windows console into UTF-8. Returns True when text will render."""
    if os.name != "nt":
        return True
    # Modern terminals are already UTF-8 capable
    if os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"):
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        if kernel32.GetConsoleOutputCP() == 65001:
            return True
        return bool(kernel32.SetConsoleOutputCP(65001))
    except Exception:  # noqa: BLE001 - no console (redirected output) or no ctypes
        return False


def _reconfigure_streams() -> None:
    """UTF-8 with replacement, so an un-encodable glyph degrades instead of raising."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - not all streams support reconfigure
            pass


def init_console() -> bool:
    renderable = _enable_windows_utf8()
    _reconfigure_streams()
    return renderable


UNICODE_OK = init_console()
SYM: dict[str, str] = _UNICODE_SYMBOLS if UNICODE_OK else _ASCII_SYMBOLS
BANNER = BANNER_UNICODE if UNICODE_OK else BANNER_ASCII

console = Console(theme=AETHER_THEME, soft_wrap=False)

# Bare aliases so f-strings don't need nested quotes: f"{ARROW} done"
ONLINE = SYM["online"]
OFFLINE = SYM["offline"]
OK = SYM["ok"]
FAIL = SYM["fail"]
ARROW = SYM["arrow"]
BULLET = SYM["bullet"]
DOT = SYM["dot"]
ELLIPSIS = SYM["ellipsis"]
PROMPT = SYM["prompt"]
DASH = SYM["dash"]
GTE = SYM["gte"]


# --- shared renderers ----------------------------------------------------

_STATUS_STYLES = {
    "COMPLETED": "ok",
    "FAILED": "err",
    "CANCELLED": "err",
    "WAITING": "warn",
    "PAUSED": "warn",
    "RUNNING": "running",
    "PLANNING": "running",
    "QUEUED": "muted",
    "CREATED": "muted",
    "HANDOFF": "running",
    "TESTING": "running",
    "REVIEW": "running",
}


def status_text(status: str) -> Text:
    """Colour a task/subtask status consistently."""
    return Text(status, style=_STATUS_STYLES.get(status.upper(), ""))


def health_text(available: bool, label: str | None = None) -> Text:
    glyph = SYM["online"] if available else SYM["offline"]
    word = label or ("ONLINE" if available else "OFFLINE")
    return Text(f"{glyph} {word}", style="ok" if available else "err")


def check_text(ok: bool) -> Text:
    return Text(SYM["ok"] if ok else SYM["fail"], style="ok" if ok else "err")


def progress_bar(fraction: float, width: int = 10) -> Text:
    """Compact inline progress bar."""
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    if UNICODE_OK:
        bar = "█" * filled + "░" * (width - filled)
    else:
        bar = "#" * filled + "-" * (width - filled)
    style = "ok" if fraction >= 1.0 else "accent"
    out = Text(bar, style=style)
    out.append(f" {fraction:>4.0%}", style="muted")
    return out


def data_table(*columns: Any, title: str | None = None, **kwargs: Any) -> Table:
    """Table with the house style: light borders, no shouty grid."""
    from rich import box

    table = Table(
        title=title,
        box=box.SIMPLE_HEAD if UNICODE_OK else box.ASCII,
        header_style="table.header",
        border_style="muted",
        title_style="heading",
        title_justify="left",
        pad_edge=False,
        **kwargs,
    )
    for column in columns:
        if isinstance(column, dict):
            table.add_column(**column)
        else:
            table.add_column(column)
    return table


def truncate(value: str, limit: int) -> str:
    value = (value or "").strip().replace("\n", " ")
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + SYM["ellipsis"]


def age(timestamp: Any) -> str:
    """Human 'time since' for table columns."""
    from datetime import datetime, timezone

    if timestamp is None:
        return "-"
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.fromisoformat(timestamp)
        except ValueError:
            return "-"
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - timestamp).total_seconds()
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"
