"""System diagnostics — aether doctor."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

from aether import __version__
from aether.app import AetherApp


async def run_doctor(app: AetherApp) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "", severity: str = "info") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "severity": severity if not ok else "ok"})

    add("python", sys.version_info >= (3, 11), f"{sys.version.split()[0]}")
    add("git", bool(shutil.which("git")), shutil.which("git") or "not found", "warn")
    add("data_dir", app.settings.data_dir.exists(), str(app.settings.data_dir))
    add("workspace_root", app.settings.workspace_root.exists(), str(app.settings.workspace_root))

    # DB
    try:
        tasks = await app.tasks.list(limit=1)
        add("database", True, f"reachable ({app.settings.database_url.split('///')[-1][:60]})")
    except Exception as exc:  # noqa: BLE001
        add("database", False, str(exc), "error")

    # providers
    health = await app.registry.health_all()
    enabled = [c for c in app.registry.list_configs() if c.enabled or c.for_testing]
    online = [h for h in health if h.available]
    add(
        "providers",
        len(online) > 0,
        f"{len(online)} online / {len(health)} registered / {len(enabled)} enabled",
        "error" if not online else "ok",
    )
    for h in health:
        add(
            f"provider:{h.provider_id}",
            h.available or h.availability.value in ("DISABLED", "ADAPTER_REQUIRED"),
            f"{h.availability.value}: {h.message}",
            "warn" if not h.available else "ok",
        )

    # API keys present (names only)
    for cfg in app.registry.list_configs():
        if cfg.api_key_env:
            present = bool(os.environ.get(cfg.api_key_env))
            add(
                f"secret_env:{cfg.api_key_env}",
                present or not cfg.enabled,
                "set" if present else ("missing (provider disabled ok)" if not cfg.enabled else "MISSING"),
                "error" if cfg.enabled and not present else "ok",
            )

    # CLI commands
    for cfg in app.registry.list_configs():
        if cfg.adapter.value == "cli" and cfg.command:
            path = shutil.which(cfg.command)
            add(
                f"cli:{cfg.provider_id}",
                bool(path) or not cfg.enabled,
                path or f"command not found: {cfg.command}",
                "error" if cfg.enabled and not path else "ok",
            )

    # write perms
    try:
        probe = app.settings.data_dir / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        add("data_writable", True, "ok")
    except OSError as exc:
        add("data_writable", False, str(exc), "error")

    ok_count = sum(1 for c in checks if c["ok"])
    err_count = sum(1 for c in checks if not c["ok"] and c["severity"] == "error")
    return {
        "version": __version__,
        "summary": {
            "checks": len(checks),
            "passed": ok_count,
            "failed": len(checks) - ok_count,
            "errors": err_count,
            "healthy": err_count == 0 and len(online) > 0,
        },
        "checks": checks,
        "providers": [h.model_dump(mode="json") for h in health],
    }
