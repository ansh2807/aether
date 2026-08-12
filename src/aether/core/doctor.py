"""System diagnostics — aether doctor."""

from __future__ import annotations

import asyncio
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

    if app.settings.security.verification_sandbox == "container":
        runtime = app.verification._container_runtime()
        if runtime is None:
            add(
                "verification_runtime",
                False,
                "Docker/Podman not found; container verification will fail closed",
                "error",
            )
        else:
            async def probe(*args: str) -> tuple[bool, str]:
                process: asyncio.subprocess.Process | None = None
                try:
                    process = await asyncio.create_subprocess_exec(
                        *args,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
                    detail = (output or b"").decode("utf-8", errors="replace").strip()
                    return process.returncode == 0, detail[:240]
                except TimeoutError:
                    if process is not None and process.returncode is None:
                        process.kill()
                        await process.wait()
                    return False, "probe timed out"
                except OSError as exc:
                    return False, str(exc)

            runtime_ok, runtime_detail = await probe(runtime, "info")
            add(
                "verification_runtime",
                runtime_ok,
                runtime_detail if not runtime_ok else f"ready ({runtime})",
                "error",
            )
            image = app.settings.security.verification_image
            if runtime_ok:
                image_ok, image_detail = await probe(runtime, "image", "inspect", image)
            else:
                image_ok, image_detail = False, "container runtime is not ready"
            add(
                "verification_image",
                image_ok,
                image if image_ok else f"{image} unavailable: {image_detail}",
                "error",
            )

    # DB
    try:
        await app.tasks.list(limit=1)  # probe: raises if the DB is unreachable
        add("database", True, f"reachable ({app.settings.database_url.split('///')[-1][:60]})")
    except Exception as exc:  # noqa: BLE001
        add("database", False, str(exc), "error")

    # providers
    health = await app.registry.health_all()
    enabled = [c for c in app.registry.list_configs() if c.enabled]
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


# --- durability self-check (WO-09) ---------------------------------------
#
# `run_doctor` answers "is this install working". This answers the different
# question "what can this install survive" — which guarantee in
# docs/DURABILITY.md holds *here*, on this machine, with this config. A
# guarantee that CI proves but your box cannot honour is worse than no
# guarantee, because you would plan around it.


async def run_durability_doctor(app: AetherApp) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(guarantee: str, ok: bool, detail: str, impact: str = "") -> None:
        checks.append(
            {
                "guarantee": guarantee,
                "ok": ok,
                "detail": detail,
                # what you lose when this one is false
                "impact": "" if ok else impact,
            }
        )

    settings = app.settings

    # 1. Task state survives a restart at all.
    try:
        await app.tasks.list(limit=1)
        add("Task state survives restart", True, f"database reachable ({settings.database_url.split('///')[-1][-48:]})")
    except Exception as exc:  # noqa: BLE001
        add(
            "Task state survives restart", False, str(exc)[:120],
            "Nothing is recoverable: tasks are not being persisted.",
        )

    # 2. Agents can write, and their output can be captured for salvage.
    workspace = Path(settings.workspace_root)
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        probe = workspace / ".durability_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        add("In-flight output is salvageable", True, f"workspace writable ({workspace})")
    except OSError as exc:
        add(
            "In-flight output is salvageable", False, f"{workspace}: {exc}",
            "A killed agent's partial work cannot be written, so it is lost.",
        )

    # 3. Control-plane state is shared between the CLI and the server.
    state_dir = Path(settings.data_dir) / "state"
    try:
        from aether.core.state_store import JsonStateStore

        store = JsonStateStore(state_dir / ".durability_probe.json")
        with store.transaction():
            store.save({"probe": True})
        store.path.unlink(missing_ok=True)
        store.lock_path.unlink(missing_ok=True)
        add("CLI and server see the same state", True, f"state dir lockable ({state_dir})")
    except Exception as exc:  # noqa: BLE001
        add(
            "CLI and server see the same state", False, f"{state_dir}: {exc}"[:120],
            "Approvals, metrics and quota will diverge between processes.",
        )

    # 4. Checkpoints need git.
    git = shutil.which("git")
    if not settings.orchestrator.enable_git_checkpoints:
        add("Work is checkpointed to git", False, "disabled in config",
            "Recovery falls back to task memory only; file-level history is lost.")
    elif git:
        add("Work is checkpointed to git", True, git)
    else:
        add("Work is checkpointed to git", False, "git not on PATH",
            "Recovery falls back to task memory only; file-level history is lost.")

    # 5. Salvage only covers adapters that stream to disk.
    capturing, buffering = [], []
    for provider_id in app.registry.list_ids():
        cfg = app.registry.get_config(provider_id)
        if cfg is None or not cfg.enabled:
            continue
        adapter = app.registry.get(provider_id)
        # CliAgentAdapter mirrors output as it arrives; the others buffer it
        (capturing if type(adapter).__name__ == "CliAgentAdapter" else buffering).append(provider_id)
    if buffering:
        add(
            "Every provider's output is captured", False,
            f"buffered in memory: {', '.join(sorted(buffering))}",
            "A kill mid-request loses in-flight output from these providers.",
        )
    else:
        add("Every provider's output is captured", True, ", ".join(sorted(capturing)) or "none enabled")

    # 6. Retry has to be switched on to absorb a blip.
    retries = settings.orchestrator.network_retries
    if retries > 0:
        add("Transient blips are retried", True, f"network_retries={retries}")
    else:
        add(
            "Transient blips are retried", False, "network_retries=0",
            "One dropped connection immediately costs a failover.",
        )

    void = [c for c in checks if not c["ok"]]
    return {
        "version": __version__,
        "summary": {
            "guarantees": len(checks),
            "holding": len(checks) - len(void),
            "void": len(void),
            "durable": not void,
        },
        "checks": checks,
    }
