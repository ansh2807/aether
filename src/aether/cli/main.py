"""AETHER CLI — terminal-first control plane."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

import typer
from rich.panel import Panel
from rich.prompt import Confirm

from aether import __version__
from aether.app import AetherApp, get_app
from aether.cli.ui import (
    DASH,
    SYM,
    age,
    check_text,
    console,
    data_table,
    health_text,
    progress_bar,
    status_text,
    truncate,
)
from aether.domain.enums import TaskMode, TaskStatus

app = typer.Typer(
    name="aether",
    help="AETHER - Universal Multi-Agent AI Orchestrator",
    no_args_is_help=False,
    add_completion=False,
)
agents_app = typer.Typer(help="Agent and provider commands")
task_app = typer.Typer(help="Task management")
checkpoint_app = typer.Typer(help="Checkpoint commands")
approvals_app = typer.Typer(help="Human approval queue")
app.add_typer(agents_app, name="agents")
app.add_typer(task_app, name="task")
app.add_typer(checkpoint_app, name="checkpoint")
app.add_typer(approvals_app, name="approvals")

def _run(coro):
    """Run a command coroutine, reporting failures as a message rather than a
    wall of traceback."""
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        console.print(f"\n[muted]{SYM['dash']} interrupted[/]")
        raise typer.Exit(130) from None
    except typer.Exit:
        raise
    except KeyError as exc:
        console.print(f"[err]Not found:[/] {exc}")
        raise typer.Exit(1) from None
    except Exception as exc:  # noqa: BLE001
        console.print(
            Panel(
                f"[err]{type(exc).__name__}[/]: {exc}\n\n"
                "[muted]Run with AETHER_DEBUG=1 for the full traceback.[/]",
                title="Command failed",
                border_style="err",
            )
        )
        if os.environ.get("AETHER_DEBUG"):
            console.print_exception()
        raise typer.Exit(1) from None


# Set by the top-level callback so `aether --config X <subcommand>` is honoured.
# A per-command --config still wins.
_global_config: Optional[str] = None


def _resolve_config(config: Optional[str] = None) -> Optional[str]:
    return config or _global_config


def _load_local_secrets() -> None:
    """Load API keys from data/.env before anything else."""
    from aether.config.session import env_path, load_dotenv_file

    data = Path.cwd() / "data"
    load_dotenv_file(env_path(data))
    # also project-root .env if present
    load_dotenv_file(Path.cwd() / ".env")


async def _boot(config: Optional[str] = None) -> AetherApp:
    _load_local_secrets()
    config = _resolve_config(config)
    application = get_app(config, force_reload=bool(config))
    await application.start()
    return application


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Show version"),
    config: Optional[str] = typer.Option(None, "--config", "-c", help="Path to config YAML"),
) -> None:
    global _global_config
    _global_config = config
    if version:
        console.print(f"AETHER {__version__}")
        raise typer.Exit()
    ctx.ensure_object(dict)
    ctx.obj["config"] = config
    if ctx.invoked_subcommand is None:
        # "Turn on" experience: onboarding wizard + interactive
        interactive(config=config)


@app.command("on")
@app.command("setup")
def setup_cmd(
    force: bool = typer.Option(False, "--force", "-f", help="Re-run full wizard"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Turn AETHER on: choose solo/multi-agent, models, keys, then connect."""

    async def _go() -> None:
        from aether.cli.onboarding import run_onboarding

        _load_local_secrets()
        app, profile = await run_onboarding(force=force, config=_resolve_config(config))
        console.print(
            f"[green]Ready.[/] mode={profile.mode.value} models={', '.join(profile.selected_providers)}"
        )
        console.print("Start chatting: [bold]aether[/]   or run: [bold]aether run \"your task\"[/]")

    _run(_go())


@app.command("connect")
def connect_cmd(
    provider: str = typer.Argument(
        "claude_cli",
        help="Provider id: claude_cli, openai, grok, gemini, codex_cli, ...",
    ),
) -> None:
    """Connect one provider via browser OAuth or API key page (like official apps)."""
    from aether.cli.connect import claude_auth_status, connect_provider

    _load_local_secrets()
    data = Path.cwd() / "data"
    data.mkdir(parents=True, exist_ok=True)

    if provider in {"claude", "claude_cli", "anthropic"}:
        provider = "claude_cli"
        st = claude_auth_status()
        if st.get("loggedIn"):
            console.print(
                Panel.fit(
                    f"[green]Already signed in to Claude[/]\n"
                    f"Email: {st.get('email')}\n"
                    f"Method: {st.get('authMethod')}\n"
                    f"Plan: {st.get('subscriptionType')}",
                    title="Claude",
                )
            )
            if not Confirm.ask("Re-login in browser anyway?", default=False):
                return

    result = connect_provider(provider, data)
    if result.ok:
        console.print(f"[ok]{SYM['ok']}[/] {result.provider_id}: [muted]{result.message}[/]")
    else:
        console.print(f"[err]{SYM['fail']}[/] {result.provider_id}: {result.message}")
        raise typer.Exit(1)


@app.command("run")
def run_task(
    prompt: str = typer.Argument(..., help="Task goal"),
    mode: str = typer.Option("solo", "--mode", "-m"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Shortcut: create and run a task (same as `aether task create`)."""
    task_create(prompt=prompt, mode=mode, config=config, wait=True)


@app.command("status")
def status_cmd(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Show system status."""

    async def _go() -> None:
        application = await _boot(config)
        tasks = await application.tasks.list(limit=50)
        health = await application.registry.health_all()
        active = [
            t
            for t in tasks
            if t.status in {TaskStatus.RUNNING, TaskStatus.PLANNING, TaskStatus.QUEUED}
        ]
        completed = sum(1 for t in tasks if t.status == TaskStatus.COMPLETED)
        failed = sum(1 for t in tasks if t.status == TaskStatus.FAILED)
        online = [h for h in health if h.available]

        console.print()
        console.print(
            Panel(
                f"[heading]AETHER[/] [muted]v{__version__}[/]\n"
                f"[muted]{application.settings.data_dir}[/]",
                border_style="accent",
                padding=(0, 2),
            )
        )

        summary = data_table(
            {"header": "Tasks", "justify": "right"},
            {"header": "Active", "justify": "right"},
            {"header": "Completed", "justify": "right"},
            {"header": "Failed", "justify": "right"},
            {"header": "Providers online", "justify": "right"},
        )
        summary.add_row(
            str(len(tasks)),
            f"[running]{len(active)}[/]" if active else "0",
            f"[ok]{completed}[/]" if completed else "0",
            f"[err]{failed}[/]" if failed else "0",
            f"{len(online)}/{len(health)}",
        )
        console.print(summary)

        providers = data_table(
            {"header": "Provider", "no_wrap": True},
            {"header": "State", "no_wrap": True},
            {"header": "Detail", "no_wrap": True, "overflow": "ellipsis"},
            title="Providers",
        )
        for h in sorted(health, key=lambda x: (not x.available, x.provider_id)):
            providers.add_row(
                h.provider_id,
                health_text(h.available, h.availability.value),
                f"[muted]{truncate(h.message, 54)}[/]",
            )
        console.print(providers)

        if active:
            running = data_table(
                {"header": "Task", "no_wrap": True},
                {"header": "Status", "no_wrap": True},
                {"header": "Progress", "no_wrap": True},
                {"header": "Agent", "no_wrap": True},
                title="In flight",
            )
            for t in active:
                running.add_row(
                    t.task_id,
                    status_text(t.status.value),
                    progress_bar(t.progress),
                    t.active_agent or f"[muted]{SYM['dash']}[/]",
                )
            console.print(running)
        console.print()

    _run(_go())


@app.command("providers")
def providers_cmd(config: Optional[str] = typer.Option(None, "--config", "-c")) -> None:
    """List configured providers."""

    async def _go() -> None:
        application = await _boot(config)
        table = data_table(
            "ID",
            "Name",
            "Adapter",
            "Model",
            {"header": "Enabled", "justify": "center"},
            "Note",
            title="Configured providers",
        )
        configs = sorted(
            application.registry.list_configs(), key=lambda c: (not c.enabled, c.provider_id)
        )
        for cfg in configs:
            note = cfg.status_note or ""
            if cfg.for_testing:
                note = note or f"testing only {DASH} never used as failover"
            # A provider taken out of rotation for a terminal failure must say
            # so. Silently dropping one is how "why is nothing using my key"
            # becomes an afternoon.
            breaker = application.circuit.snapshot(cfg.provider_id)
            if breaker.disabled_reason:
                note = breaker.disabled_reason
            table.add_row(
                f"[value]{cfg.provider_id}[/]" if cfg.enabled else f"[muted]{cfg.provider_id}[/]",
                cfg.display_name,
                cfg.adapter.value,
                truncate(cfg.model or cfg.command or "-", 28),
                check_text(cfg.enabled),
                f"[muted]{truncate(note, 44)}[/]",
            )
        console.print(table)
        console.print("[muted]Enable a provider in config, then:[/] [accent]aether connect <id>[/]")

    _run(_go())


@app.command("models")
def models_cmd(
    kind: Optional[str] = typer.Option(
        None, "--kind", "-k", help="cli | aggregator | api | local"
    ),
    search: Optional[str] = typer.Option(None, "--search", "-s", help="Filter by name or id"),
    ready_only: bool = typer.Option(
        False, "--ready", help="Only providers this machine can already reach"
    ),
) -> None:
    """Every model provider AETHER can connect to, and which are ready here.

    Reads no database and boots nothing, so it works before `aether setup`.
    """
    from aether.config.providers import KIND_LABEL, KIND_ORDER, is_ready, selectable

    _load_local_secrets()

    needle = (search or "").lower()
    shown = 0

    for k in KIND_ORDER:
        if kind and kind != k:
            continue
        group = [p for p in selectable() if p.kind == k]
        if needle:
            group = [p for p in group if needle in p.id.lower() or needle in p.name.lower()]

        rows = []
        for p in group:
            ready = is_ready(p)
            if p.kind == "cli":
                how = f"installed: {p.command}" if ready else f"install the {p.command} CLI"
            elif p.kind == "local":
                how = "server running" if ready else f"start a server at {p.base_url}"
            else:
                how = f"key set ({p.api_key_env})" if ready else f"needs ${p.api_key_env}"
            if ready_only and not ready:
                continue
            rows.append((p, ready, how))

        if not rows:
            continue

        table = data_table(
            "ID",
            "Provider",
            "Default model",
            {"header": "Ready", "justify": "center"},
            "How",
            title=KIND_LABEL[k],
        )
        for p, ready, how in rows:
            table.add_row(
                f"[value]{p.id}[/]" if ready else f"[muted]{p.id}[/]",
                p.name,
                truncate(p.default_model or "-", 30),
                check_text(ready),
                f"[muted]{truncate(how, 34)}[/]",
            )
        console.print(table)
        shown += len(rows)

    if not shown:
        console.print("[muted]Nothing matched that filter.[/]")
        return

    noun = "provider" if shown == 1 else "providers"
    console.print(f"\n[muted]{shown} {noun} listed. Connect one:[/] [accent]aether connect <id>[/]")
    console.print(
        "[muted]'Ready' means the key or CLI is present on this machine, not that the\n"
        "endpoint has been called. `aether connect` is what proves it works.[/]"
    )
    if not (ready_only or kind or search):
        unverified = [p for p in selectable() if not p.verified]
        console.print(
            f"[muted]{len(unverified)} entries were taken from vendor docs and never "
            "exercised from here — expect the occasional stale endpoint.[/]"
        )


@app.command("logs")
def logs_cmd(
    task_id: Optional[str] = typer.Argument(None),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    limit: int = typer.Option(50, "--limit", "-n"),
) -> None:
    """Show timeline logs for a task (or audit tail)."""

    async def _go() -> None:
        application = await _boot(config)
        if task_id:
            events = await application.repo.list_events(task_id, limit=limit)
            for e in events:
                console.print(f"[cyan]{e.timestamp.isoformat()}[/] [bold]{e.actor}[/] {e.action}: {e.detail}")
        else:
            audit = application.settings.data_dir / "audit" / "audit.jsonl"
            if not audit.exists():
                console.print("No audit log yet.")
                return
            lines = audit.read_text(encoding="utf-8").splitlines()[-limit:]
            for line in lines:
                console.print(line)

    _run(_go())


@app.command("serve")
def serve_cmd(
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    setup: bool = typer.Option(False, "--setup", help="Run model setup wizard first"),
) -> None:
    """Start API + WebSocket server."""
    import uvicorn

    from aether.api.server import create_fastapi_app

    _load_local_secrets()
    if setup:

        async def _setup_boot():
            from aether.cli.onboarding import run_onboarding

            return await run_onboarding(force=False, config=_resolve_config(config))

        application, _ = _run(_setup_boot())
    else:
        # Prefer user config written by onboarding
        user_cfg = Path.cwd() / "config" / "aether.yaml"
        cfg = _resolve_config(config) or (str(user_cfg) if user_cfg.exists() else None)
        application = get_app(cfg, force_reload=True)
    h = host or application.settings.server.host
    p = port or application.settings.server.port
    console.print(f"[bold green]Starting AETHER API on http://{h}:{p}[/]")

    if application.settings.server.require_auth:
        from aether.api.auth import load_or_create_token, token_path

        token = application.settings.server.api_token or load_or_create_token(
            Path(application.settings.data_dir)
        )
        from aether.api.server import dashboard_dir

        if dashboard_dir(application.settings) is not None:
            dashboard = f"http://{h}:{p}/?token={token}"
        else:
            dashboard = (
                "not built [dim](cd web && npm install && npm run build, then restart)[/]"
            )
        console.print(
            Panel.fit(
                f"[bold]API token[/]  [cyan]{token}[/]\n\n"
                f"Dashboard: {dashboard}\n"
                f"curl:      -H \"Authorization: Bearer {token}\"\n"
                f"Stored in: {token_path(Path(application.settings.data_dir))}\n\n"
                f"[dim]The API can run agents and read provider keys {DASH} keep this token local.\n"
                "Disable with server.require_auth: false (not recommended).[/]",
                border_style="yellow",
                title="Authentication required",
            )
        )

    fastapi_app = create_fastapi_app(application)
    uvicorn.run(fastapi_app, host=h, port=p, log_level="info")


@app.command("interactive")
@app.command("term")
@app.command("shell")
def interactive(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    skip_setup: bool = typer.Option(False, "--skip-setup", help="Skip onboarding wizard"),
) -> None:
    """Agent-style terminal session (login, then an agent REPL)."""

    async def _go() -> None:
        from aether.cli.onboarding import run_onboarding
        from aether.cli.terminal import run_terminal
        from aether.config.session import load_profile

        _load_local_secrets()
        cfg = _resolve_config(config)
        if skip_setup:
            application = await _boot(cfg)
            profile = load_profile(Path.cwd() / "data")
        else:
            application, profile = await run_onboarding(force=False, config=cfg)

        await run_terminal(application, profile, config=cfg)

    _run(_go())


# ---- task commands ----


@task_app.callback(invoke_without_command=True)
def task_root(ctx: typer.Context) -> None:
    """Task management commands."""
    if ctx.invoked_subcommand is None:
        console.print(
            'Usage:\n'
            '  aether task create "your goal"\n'
            '  aether task list\n'
            '  aether task inspect TASK-...\n'
            '  aether task pause|resume|cancel TASK-...'
        )


@task_app.command("create")
def task_create(
    prompt: str = typer.Argument(..., help="Task goal / prompt"),
    mode: str = typer.Option("solo", "--mode", "-m"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    wait: bool = typer.Option(True, "--wait/--no-wait"),
) -> None:
    """Create and start a task."""

    async def _go() -> None:
        application = await _boot(config)
        try:
            m = TaskMode(mode)
        except ValueError:
            valid = ", ".join(x.value for x in TaskMode)
            console.print(f"[err]Unknown mode[/] '{mode}'.\n[muted]Valid: {valid}[/]")
            raise typer.Exit(2) from None

        task = await application.orchestrator.submit(prompt, mode=m, auto_start=True)
        console.print()
        console.print(
            Panel(
                f"[value]{truncate(prompt, 68)}[/]\n"
                f"[muted]{task.task_id}  {SYM['dot']}  mode={m.value}[/]",
                title="Task started",
                border_style="accent",
                padding=(0, 2),
            )
        )
        if not wait:
            return

        final = await _follow_task(application, task.task_id)
        _print_task_result(final)

    _run(_go())


async def _follow_task(application: AetherApp, task_id: str, timeout: float = 600.0):
    """Live status while a task runs, with the timeline streaming underneath."""
    from rich.live import Live
    from rich.console import Group
    from rich.spinner import Spinner

    terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.PAUSED}
    seen = 0
    recent: list[str] = []
    spinner = Spinner("dots", style="accent")
    deadline = asyncio.get_event_loop().time() + timeout
    task = await application.tasks.get(task_id)

    def render():
        header = data_table(
            "Status", "Progress", "Agent", {"header": "Subtasks", "justify": "right"}
        )
        done = sum(1 for s in task.subtasks if s.status.value in {"COMPLETED", "SKIPPED"})
        header.add_row(
            status_text(task.status.value),
            progress_bar(task.progress),
            task.active_agent or f"[muted]{SYM['dash']}[/]",
            f"{done}/{len(task.subtasks)}" if task.subtasks else f"[muted]{SYM['dash']}[/]",
        )
        lines = [f"[muted]{line}[/]" for line in recent[-8:]]
        body = [header]
        if lines:
            body.append(Group(*[console.render_str(line) for line in lines]))
        if task.status not in terminal:
            body.append(spinner)
        return Group(*body)

    with Live(render(), console=console, refresh_per_second=12, transient=True) as live:
        while asyncio.get_event_loop().time() < deadline:
            task = await application.tasks.get(task_id)
            if task is None:
                break
            events = await application.repo.list_events(task_id, limit=400)
            for event in events[seen:]:
                recent.append(
                    f"  {SYM['dot']} {event.actor} {event.action}: {truncate(event.detail, 62)}"
                )
            seen = len(events)
            live.update(render())
            if task.status in terminal:
                break
            await asyncio.sleep(0.2)

    for line in recent:
        console.print(line, highlight=False)
    return task


def _print_task_result(task) -> None:
    if task is None:
        console.print("[err]Task disappeared.[/]")
        return
    style = {
        TaskStatus.COMPLETED: "ok",
        TaskStatus.FAILED: "err",
        TaskStatus.CANCELLED: "err",
    }.get(task.status, "warn")
    glyph = SYM["ok"] if task.status == TaskStatus.COMPLETED else SYM["fail"]
    console.print()
    console.print(
        f"[{style}]{glyph} {task.status.value}[/]  "
        f"[muted]{task.task_id}  {SYM['dot']}  {task.progress:.0%}  "
        f"{SYM['dot']}  agents: {', '.join(task.participating_agents) or 'none'}[/]"
    )
    if task.failure_reason:
        console.print(f"[err]{truncate(task.failure_reason, 200)}[/]")
    if task.workspace_path:
        console.print(f"[muted]workspace:[/] {task.workspace_path}")
    if task.final_output:
        console.print(
            Panel(
                task.final_output[:2500],
                title="Result",
                border_style=style,
                padding=(0, 2),
            )
        )


@task_app.command("list")
def task_list(
    status: Optional[str] = typer.Option(None, "--status"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    async def _go() -> None:
        application = await _boot(config)
        try:
            st = TaskStatus(status.upper()) if status else None
        except ValueError:
            valid = ", ".join(s.value for s in TaskStatus)
            console.print(f"[err]Unknown status[/] '{status}'.\n[muted]Valid: {valid}[/]")
            raise typer.Exit(2) from None
        tasks = await application.tasks.list(status=st, limit=50)
        if not tasks:
            console.print('[muted]No tasks yet. Start one:[/] [accent]aether run "your goal"[/]')
            return
        # fit the terminal: on a narrow console the bar becomes a plain
        # percentage and optional columns drop, so the task id stays readable
        narrow = console.width < 100
        show_agent = any(t.active_agent for t in tasks)
        bar_width = 10

        # fixed widths stop Rich shrinking the identifying columns to fit a
        # long goal; Goal itself absorbs the remainder and ellipsises
        columns: list[dict] = [
            {"header": "Task", "no_wrap": True, "width": 19},
            {"header": "Status", "no_wrap": True, "width": 9},
            {
                "header": "%" if narrow else "Progress",
                "no_wrap": True,
                "width": 4 if narrow else bar_width + 5,
                "justify": "right" if narrow else "left",
            },
        ]
        if show_agent:
            columns.append(
                {"header": "Agent", "no_wrap": True, "width": 12, "overflow": "ellipsis"}
            )
        if not narrow:
            columns.append(
                {"header": "Age", "justify": "right", "no_wrap": True, "width": 4}
            )
        columns.append({"header": "Goal", "no_wrap": True, "overflow": "ellipsis"})

        table = data_table(*columns, title=f"Tasks ({len(tasks)})")
        for t in tasks:
            progress = (
                f"[muted]{t.progress:.0%}[/]" if narrow else progress_bar(t.progress, bar_width)
            )
            row = [t.task_id, status_text(t.status.value), progress]
            if show_agent:
                row.append(t.active_agent or f"[muted]{DASH}[/]")
            if not narrow:
                row.append(f"[muted]{age(t.updated_at)}[/]")
            row.append(truncate(t.normalized_goal, 60))
            table.add_row(*row)
        console.print(table)

    _run(_go())


@task_app.command("inspect")
def task_inspect(
    task_id: str = typer.Argument(...),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    async def _go() -> None:
        application = await _boot(config)
        data = await application.orchestrator.inspect(task_id)
        if json_out:
            console.print_json(json.dumps(data, default=str))
            return
        task = data["task"]
        memory = data["memory"] or {}
        console.print(
            Panel(
                f"[bold]{task['task_id']}[/]\n"
                f"Status: {task['status']}\n"
                f"Progress: {task['progress']:.0%}\n"
                f"Mode: {task['mode']}\n"
                f"Active agent: {task.get('active_agent') or '-'}\n"
                f"Agents: {', '.join(task.get('participating_agents') or []) or '-'}\n"
                f"Goal: {task.get('normalized_goal')}\n\n"
                f"State: {memory.get('current_state')}\n"
                f"Last agent: {memory.get('last_agent')}\n"
                f"Next: {memory.get('next_action')}",
                title="Task Inspect",
            )
        )
        console.print("[bold]Timeline[/]")
        for e in data["timeline"][-20:]:
            console.print(f"  {e['timestamp']} {e['actor']} {e['action']}: {e['detail'][:100]}")

    _run(_go())


@task_app.command("pause")
def task_pause(task_id: str, config: Optional[str] = typer.Option(None, "--config", "-c")) -> None:
    async def _go() -> None:
        application = await _boot(config)
        t = await application.orchestrator.pause_task(task_id)
        console.print(f"[warn]Paused[/] {t.task_id} {SYM['arrow']} ", status_text(t.status.value))

    _run(_go())


@task_app.command("resume")
def task_resume(task_id: str, config: Optional[str] = typer.Option(None, "--config", "-c")) -> None:
    async def _go() -> None:
        application = await _boot(config)
        t = await application.orchestrator.resume_task(task_id)
        console.print(f"[ok]Resumed[/] {t.task_id} {SYM['arrow']} ", status_text(t.status.value))

    _run(_go())


@task_app.command("cancel")
def task_cancel(task_id: str, config: Optional[str] = typer.Option(None, "--config", "-c")) -> None:
    async def _go() -> None:
        application = await _boot(config)
        t = await application.orchestrator.cancel_task(task_id)
        console.print(f"[err]Cancelled[/] {t.task_id} {SYM['arrow']} ", status_text(t.status.value))

    _run(_go())


# ---- agents ----


@agents_app.callback(invoke_without_command=True)
def agents_root(ctx: typer.Context, config: Optional[str] = typer.Option(None, "--config", "-c")) -> None:
    if ctx.invoked_subcommand is not None:
        return

    async def _go() -> None:
        application = await _boot(config)
        table = data_table(
            "Provider", "Adapter", {"header": "Enabled", "justify": "center"}, "Roles",
            title="Registered agents",
        )
        registered = set(application.registry.list_ids())
        for cfg in application.registry.list_configs():
            if cfg.provider_id in registered:
                table.add_row(
                    cfg.provider_id,
                    cfg.adapter.value,
                    check_text(cfg.enabled),
                    f"[muted]{truncate(', '.join(cfg.roles_preferred) or '-', 48)}[/]",
                )
        console.print(table)

    _run(_go())


@agents_app.command("health")
def agents_health(config: Optional[str] = typer.Option(None, "--config", "-c")) -> None:
    async def _go() -> None:
        application = await _boot(config)
        health = await application.registry.health_all()
        circuits = {c.provider_id: c for c in application.circuit.all_snapshots()}
        table = data_table(
            "Provider",
            "Status",
            "Availability",
            {"header": "Latency", "justify": "right"},
            "Circuit",
            "Message",
            title="Agent health",
        )
        for h in sorted(health, key=lambda x: (not x.available, x.provider_id)):
            snap = circuits.get(h.provider_id)
            if snap is None or snap.state.value == "CLOSED":
                circuit = f"[muted]{SYM['dash']}[/]"
            else:
                circuit = f"[warn]{snap.state.value} ({snap.failure_count})[/]"
            table.add_row(
                h.provider_id,
                health_text(h.available),
                h.availability.value,
                f"{h.latency_ms:.0f}ms" if h.latency_ms is not None else f"[muted]{SYM['dash']}[/]",
                circuit,
                f"[muted]{truncate(h.message, 46)}[/]",
            )
        console.print(table)

    _run(_go())


@agents_app.command("test")
def agents_test(
    provider: str = typer.Argument("mock"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Run a smoke execute against a provider."""

    async def _go() -> None:
        application = await _boot(config)
        adapter = application.registry.get(provider)
        if adapter is None:
            console.print(f"[red]Unknown provider:[/] {provider}")
            raise typer.Exit(1)
        from aether.domain.models import ExecutionRequest

        ws = application.settings.workspace_root / "_smoke"
        ws.mkdir(parents=True, exist_ok=True)
        req = ExecutionRequest(
            task_id="SMOKE",
            provider_id=provider,
            prompt="STATUS: COMPLETE\nCOMPLETED:\n- smoke\nREMAINING:\n- none\nRECOMMENDED_NEXT_ACTION:\ndone\n",
            workspace_path=str(ws),
            timeout_seconds=60,
        )
        result = await adapter.execute(req)
        console.print(f"success={result.success} latency_ms={result.latency_ms:.1f}")
        if result.error:
            console.print(f"[red]{result.error}[/]")
        else:
            console.print(result.output[:500])

    _run(_go())


@checkpoint_app.command("list")
def checkpoint_list(
    task_id: str = typer.Argument(...),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    async def _go() -> None:
        application = await _boot(config)
        cps = await application.checkpoints.list(task_id)
        if not cps:
            console.print(f"[muted]No checkpoints for {task_id}.[/]")
            return
        table = data_table(
            "Checkpoint", "Agent", "Git", {"header": "Age", "justify": "right"}, "Summary",
            title=f"Checkpoints {SYM['dot']} {task_id}",
        )
        for c in cps:
            table.add_row(
                c.checkpoint_id,
                c.agent_id or f"[muted]{SYM['dash']}[/]",
                f"[muted]{(c.git_commit or SYM['dash'])[:8]}[/]",
                f"[muted]{age(c.created_at)}[/]",
                truncate(c.state_summary, 48),
            )
        console.print(table)

    _run(_go())


@app.command("doctor")
def doctor_cmd(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    durability: bool = typer.Option(
        False, "--durability", help="Report which durability guarantees hold on THIS install"
    ),
) -> None:
    """Run system diagnostics."""

    async def _durability() -> None:
        from aether.core.doctor import run_durability_doctor

        application = await _boot(config)
        report = await run_durability_doctor(application)
        summary = report["summary"]
        durable = bool(summary["durable"])

        table = data_table(
            "",
            {"header": "Guarantee", "no_wrap": True},
            {"header": "Detail", "no_wrap": True, "overflow": "ellipsis"},
            title="Durability on this install",
            show_header=False,
        )
        for c in report["checks"]:
            table.add_row(
                check_text(bool(c["ok"])),
                c["guarantee"] if c["ok"] else f"[warn]{c['guarantee']}[/]",
                f"[muted]{truncate(str(c['detail']), 56)}[/]",
            )
        console.print()
        console.print(table)

        # A void guarantee is only useful if you know what it costs you.
        for c in report["checks"]:
            if not c["ok"] and c["impact"]:
                console.print(f"  [err]{SYM['fail']}[/] [warn]{c['guarantee']}[/] {DASH} {c['impact']}")

        console.print(
            Panel(
                f"[{'ok' if durable else 'warn'}]"
                f"{SYM['ok'] if durable else SYM['warn']} "
                f"{summary['holding']}/{summary['guarantees']} guarantees hold[/]\n"
                f"[muted]Full list and what proves each: docs/DURABILITY.md[/]",
                border_style="ok" if durable else "warn",
                padding=(0, 2),
            )
        )
        if not durable:
            raise typer.Exit(1)

    if durability:
        _run(_durability())
        return

    async def _go() -> None:
        from aether.core.doctor import run_doctor

        application = await _boot(config)
        report = await run_doctor(application)
        summary = report["summary"]
        healthy = bool(summary["healthy"])

        problems = [c for c in report["checks"] if not c["ok"]]
        table = data_table("", "Check", "Detail", title="Diagnostics", show_header=False)
        for c in report["checks"]:
            detail = str(c["detail"])
            table.add_row(
                check_text(bool(c["ok"])),
                c["name"] if c["ok"] else f"[warn]{c['name']}[/]",
                f"[muted]{truncate(detail, 62)}[/]" if c["ok"] else truncate(detail, 62),
            )
        console.print()
        console.print(table)
        console.print(
            Panel(
                f"[{'ok' if healthy else 'err'}]"
                f"{SYM['ok'] if healthy else SYM['fail']} "
                f"{'All systems go' if healthy else 'Problems found'}[/]\n"
                f"[muted]{summary['passed']}/{summary['checks']} checks passed"
                + (f"  {SYM['dot']}  {summary['errors']} error(s)" if summary["errors"] else "")
                + "[/]",
                title=f"Doctor {SYM['dot']} v{report['version']}",
                border_style="ok" if healthy else "err",
                padding=(0, 2),
            )
        )
        if problems and not healthy:
            console.print("[muted]Fix the highlighted rows above, then re-run [/][accent]aether doctor[/]")
        if not healthy:
            raise typer.Exit(1)

    _run(_go())


@app.command("metrics")
def metrics_cmd(
    task_id: Optional[str] = typer.Argument(None),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Show orchestrator metrics or a task trace."""

    async def _go() -> None:
        application = await _boot(config)
        if task_id:
            console.print_json(
                json.dumps(
                    {
                        "trace": application.metrics.task_trace(task_id),
                        "budget": application.budgets.snapshot(task_id),
                    },
                    default=str,
                )
            )
        else:
            console.print_json(json.dumps(application.metrics.summary(), default=str))

    _run(_go())


@task_app.command("export")
def task_export(
    task_id: str = typer.Argument(...),
    out: Path = typer.Option(Path("task-export.json"), "--out", "-o"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Export task state to JSON for continuity/backup."""

    async def _go() -> None:
        application = await _boot(config)
        path = await application.exporter.export_json(task_id, out)
        console.print(f"[ok]{SYM['ok']} Exported[/] {task_id} [muted]{SYM['arrow']} {path}[/]")

    _run(_go())


@task_app.command("import")
def task_import(
    path: Path = typer.Argument(...),
    resume: bool = typer.Option(False, "--resume", help="Resume after import"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Import a previously exported task."""

    async def _go() -> None:
        application = await _boot(config)
        task = await application.exporter.import_json(path, as_paused=not resume)
        console.print(f"[green]Imported[/] {task.task_id} status={task.status.value}")
        if resume:
            await application.orchestrator.resume_task(task.task_id)
            console.print("Resumed.")

    _run(_go())


@task_app.command("trace")
def task_trace(
    task_id: str = typer.Argument(...),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Show metrics trace for a task."""

    async def _go() -> None:
        application = await _boot(config)
        for row in application.metrics.task_trace(task_id):
            console.print(f"{row.get('ts')} {row.get('event')} { {k:v for k,v in row.items() if k not in ('ts','event')} }")
        budget = application.budgets.snapshot(task_id)
        if budget:
            console.print(Panel(json.dumps(budget, indent=2), title="Budget"))

    _run(_go())


@approvals_app.callback(invoke_without_command=True)
def approvals_root(
    ctx: typer.Context,
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    if ctx.invoked_subcommand is not None:
        return

    async def _go() -> None:
        application = await _boot(config)
        pending = application.approvals.list_pending()
        if not pending:
            console.print(f"[ok]{SYM['ok']}[/] [muted]No pending approvals.[/]")
            return
        risk_style = {"high": "err", "medium": "warn", "low": "muted"}
        table = data_table(
            "Approval", "Task", "Operation", "Risk", "Detail",
            title=f"Pending approvals ({len(pending)})",
        )
        for a in pending:
            table.add_row(
                f"[value]{a.approval_id}[/]",
                a.task_id,
                a.operation,
                f"[{risk_style.get(a.risk, 'muted')}]{a.risk}[/]",
                f"[muted]{truncate(a.detail, 44)}[/]",
            )
        console.print(table)
        console.print(
            f"[muted]Resolve with[/] [accent]aether approvals grant {pending[0].approval_id}[/]"
            f"[muted] (or deny)[/]"
        )

    _run(_go())


@approvals_app.command("grant")
def approvals_grant(
    approval_id: str = typer.Argument(...),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    async def _go() -> None:
        application = await _boot(config)
        req = application.approvals.get(approval_id)
        if req:
            application.sandbox.grant(req.operation)
        result = await application.orchestrator.resolve_approval(approval_id, granted=True)
        console.print(f"[green]Granted[/] {result['approval_id']} task={result['task_id']}")

    _run(_go())


@approvals_app.command("deny")
def approvals_deny(
    approval_id: str = typer.Argument(...),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    async def _go() -> None:
        application = await _boot(config)
        result = await application.orchestrator.resolve_approval(approval_id, granted=False, auto_resume=False)
        console.print(f"[red]Denied[/] {result['approval_id']}")

    _run(_go())


if __name__ == "__main__":
    app()
