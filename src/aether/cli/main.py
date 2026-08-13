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


async def _boot(config: Optional[str] = None, *, demo: bool = False) -> AetherApp:
    _load_local_secrets()
    config = _resolve_config(config)
    application = get_app(config, force_reload=bool(config))
    if demo:
        mock = application.registry.get_config("mock")
        if mock is None:
            raise RuntimeError("Demo mode requires the bundled mock provider")
        mock.enabled = True
        application.settings.routing.allow_testing_fallback = True
        console.print(
            "[warn]DEMO MODE[/] [muted]The mock provider fabricates output; "
            "results are not real work.[/]"
        )
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
    demo: bool = typer.Option(False, "--demo", help="Use simulated mock output"),
) -> None:
    """Shortcut: create and run a task (same as `aether task create`)."""
    task_create(prompt=prompt, mode=mode, config=config, wait=True, demo=demo)


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


agentic_app = typer.Typer(help="Agentic view: staff a company of specialists onto your models")
app.add_typer(agentic_app, name="agentic")


def _roster_or_exit(path: Optional[str] = None):
    from aether.agentic import Roster

    roster = Roster.discover(path)
    if roster is None:
        console.print(
            Panel(
                "No agent roster found.\n\n"
                "The agentic view staffs a company of named specialists onto your\n"
                "models. It needs a roster directory containing\n"
                "[value]digital-wizards-roster.json[/] and [value].claude/agents/[/].\n\n"
                "Looked in: ~/Desktop/agentic ai, ~/agentic ai, ./agentic ai\n"
                "Point at one with [accent]--roster <path>[/].",
                title="No roster",
                border_style="warn",
            )
        )
        raise typer.Exit(1)
    return roster


@agentic_app.command("org")
def agentic_org(
    division: Optional[str] = typer.Option(None, "--division", "-d"),
    roster_path: Optional[str] = typer.Option(None, "--roster"),
) -> None:
    """The company: divisions, headcount, who is in them."""
    from aether.agentic.view import render_org

    render_org(_roster_or_exit(roster_path), division)


@agentic_app.command("who")
def agentic_who(
    query: str = typer.Argument(..., help="Skill, division or name to search for"),
    roster_path: Optional[str] = typer.Option(None, "--roster"),
) -> None:
    """Find the specialist for a job."""
    roster = _roster_or_exit(roster_path)
    hits = roster.search(query)
    if not hits:
        console.print(f"[muted]Nobody matches '{query}'.[/]")
        return
    table = data_table("Agent", "Division", "Expertise", title=f"Matches for '{query}'")
    for p in hits:
        table.add_row(f"[value]{p.name}[/]", f"[muted]{p.division}[/]",
                      f"[muted]{truncate(p.description, 70)}[/]")
    console.print(table)


@agentic_app.command("plan")
def agentic_plan(
    goal: str = typer.Argument(..., help="What you want the company to do"),
    roster_path: Optional[str] = typer.Option(None, "--roster"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Approve and run without asking"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the staffing and stop"),
) -> None:
    """Plan a goal, staff it across the company, then ask before running.

    The permission gate is the point. Staffing is deterministic and only
    approximately right about which individual takes a subtask, so the plan is
    shown — with its reasoning — and nothing runs until you say so.
    """
    from aether.agentic.staffing import staff_task
    from aether.agentic.view import render_plan

    roster = _roster_or_exit(roster_path)

    async def _go() -> None:
        application = await _boot(config)
        task = await application.orchestrator.submit(
            goal, mode=TaskMode.SOLO, auto_start=False
        )
        # Plan without starting. `start_task` normally does both, but the whole
        # point here is to see the staffing before anything runs.
        orch = application.orchestrator
        plan = await orch.workflow.plan_task(
            task, orch._select_planning_adapter(), application.settings
        )
        if not plan.subtasks:
            console.print("[warn]The planner produced no subtasks.[/]")
            raise typer.Exit(1)
        await application.tasks.update(
            task.task_id,
            lambda t: (
                t.subtasks.extend(plan.subtasks),
                t.metadata.update({"planner_source": plan.source}),
            ),
        )
        task = await application.tasks.get(task.task_id)

        assignments = staff_task(roster, task.subtasks, goal)
        render_plan(assignments, goal)

        # Staffing can only be as specific as the plan it is staffing. The
        # keyword fallback emits "Plan approach / Implement / Verify", which
        # names no domain at all — every specialist matches equally badly and
        # the result looks like the matcher is broken when the plan is what is
        # thin. Say so rather than let the output be quietly misread.
        if plan.source != "llm":
            console.print(
                f"[warn]Planned without a model[/] [muted]({plan.source}"
                f"{': ' + plan.reason if plan.reason else ''}). The generic subtasks "
                f"above name no\ndomain, so staffing has little to match on. Connect a "
                f"provider for a real plan:[/]\n  [accent]aether models --ready[/]"
            )

        if dry_run:
            console.print(f"\n[muted]Dry run {DASH} nothing was started. Task "
                          f"{task.task_id} is queued.[/]")
            return

        if not yes and not Confirm.ask("\nApprove this staffing and start work?", default=False):
            console.print(f"[muted]Not started. The plan is kept as {task.task_id}; "
                          f"re-run with --yes to approve.[/]")
            return

        # Persist the staffing so the orchestrator can put each persona in
        # front of its subtask, and so `agentic board` can show the floor.
        staffed = {
            a.subtask_id: a.persona.slug for a in assignments if a.persona is not None
        }
        await application.tasks.update(
            task.task_id,
            lambda t: t.metadata.update(
                {"agentic_roster": str(roster.root), "agentic_staffing": staffed}
            ),
        )
        console.print(f"[ok]{SYM['ok']}[/] Approved. Starting {len(staffed)} staffed subtasks.")
        await application.orchestrator.start_task(task.task_id)
        console.print(f"[muted]Watch it:[/] [accent]aether agentic board {task.task_id}[/]")

    _run(_go())


@agentic_app.command("memory")
def agentic_memory(
    agent: Optional[str] = typer.Argument(None, help="Persona slug, or omit for all"),
    forget: bool = typer.Option(False, "--forget", help="Clear this agent's memory (or all)"),
    roster_path: Optional[str] = typer.Option(None, "--roster"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """What each specialist has learned across tasks.

    Separate from task memory, which dies with its task. This is what the
    Database Optimizer knows about *your repo* after working on it.
    """
    from aether.agentic.memory import AgentMemoryStore

    roster = _roster_or_exit(roster_path)

    async def _go() -> None:
        application = await _boot(config)
        store = AgentMemoryStore(
            Path(application.settings.data_dir) / "state" / "agent_memory.json"
        )

        if forget:
            target = agent or None
            who = roster.get(agent).name if agent and roster.get(agent) else (agent or "everyone")
            if not Confirm.ask(f"Erase remembered notes for {who}?", default=False):
                console.print("[muted]Kept.[/]")
                return
            store.forget(target)
            console.print(f"[ok]{SYM['ok']}[/] Forgotten: {who}")
            return

        if agent:
            persona = roster.get(agent)
            entries = store.recall(agent)
            if not entries:
                console.print(f"[muted]{agent} has not recorded anything yet.[/]")
                return
            console.print(Panel(f"[heading]{persona.name if persona else agent}[/]",
                                border_style="accent", padding=(0, 2)))
            table = data_table("When", "Task", "Outcome", "Model", "Note")
            for e in entries:
                table.add_row(e.at[:16], truncate(e.subtask, 26),
                              "[ok]ok[/]" if e.succeeded else f"[warn]{e.outcome}[/]",
                              e.provider or "-", f"[muted]{truncate(e.note, 52)}[/]")
            console.print(table)
            return

        counts = store.counts()
        if not counts:
            console.print("[muted]Nobody has learned anything yet. Memory is written "
                          "when a staffed subtask completes.[/]")
            return
        table = data_table("Agent", "Division", {"header": "Notes", "justify": "right"},
                           title="What the company remembers")
        for slug, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            p = roster.get(slug)
            table.add_row(f"[value]{p.name if p else slug}[/]",
                          f"[muted]{p.division if p else '-'}[/]", str(n))
        console.print(table)
        console.print("\n[muted]One agent:[/] [accent]aether agentic memory <slug>[/]")

    _run(_go())


@agentic_app.command("board")
def agentic_board(
    task_id: str = typer.Argument(..., help="Task to show the floor for"),
    roster_path: Optional[str] = typer.Option(None, "--roster"),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
) -> None:
    """Who is working on what, right now."""
    from aether.agentic.staffing import Assignment
    from aether.agentic.view import render_board

    roster = _roster_or_exit(roster_path)

    async def _go() -> None:
        application = await _boot(config)
        task = await application.tasks.get(task_id)
        if task is None:
            console.print(f"[err]No task {task_id}[/]")
            raise typer.Exit(1)

        staffing = (task.metadata or {}).get("agentic_staffing") or {}
        if not staffing:
            console.print(
                f"[muted]{task_id} was not staffed through the agentic view. "
                f"Use[/] [accent]aether agentic plan \"...\"[/]"
            )
            return

        assignments, statuses, providers = [], {}, {}
        for sub in task.subtasks:
            slug = staffing.get(sub.subtask_id)
            assignments.append(
                Assignment(
                    subtask_id=sub.subtask_id,
                    subtask_title=sub.title,
                    role=sub.role,
                    persona=roster.get(slug) if slug else None,
                    score=0.0,
                )
            )
            statuses[sub.subtask_id] = sub.status.value
            providers[sub.subtask_id] = sub.assigned_agent or "-"

        console.print(Panel(f"[heading]{truncate(task.normalized_goal or task.original_prompt, 90)}[/]\n"
                            f"[muted]{task_id} {DASH} {task.status.value}[/]",
                            border_style="accent", padding=(0, 2)))
        render_board(roster, assignments, statuses, providers)

    _run(_go())


@app.command("skills")
def skills_cmd(
    path: Optional[str] = typer.Option(None, "--path", "-p", help="A skill library directory"),
    search: Optional[str] = typer.Option(None, "--search", "-s"),
    show: Optional[str] = typer.Option(None, "--show", help="Print one skill's instructions"),
) -> None:
    """Skills AETHER can apply — its own, plus any library you point it at.

    Nothing external loads unless you pass --path. Offensive skills inside a
    loaded library are listed and readable, but are never applied to a task
    automatically.
    """
    from aether.skills.catalog import DEFENSIVE, DUAL_USE, OFFENSIVE, SkillCatalog

    console.print("[value]secure-code-review[/] "
                  "[muted]built in " + DASH + " defensive " + DASH + " gates verification[/]")

    if not path:
        console.print("\n[muted]Load a library:[/] [accent]aether skills --path <dir>[/]")
        console.print("[muted]e.g. github.com/mukul975/Anthropic-Cybersecurity-Skills[/]")
        return

    catalog = SkillCatalog.discover(path)
    if catalog is None:
        console.print(f"[err]No SKILL.md files under[/] {path}")
        raise typer.Exit(1)

    if show:
        skill = catalog.get(show)
        if skill is None:
            console.print(f"[err]No skill named[/] {show}")
            raise typer.Exit(1)
        console.print(Panel(f"[heading]{skill.name}[/]\n[muted]{skill.description}[/]",
                            border_style="accent", padding=(0, 2)))
        console.print(skill.body())
        return

    if search:
        table = data_table("Skill", "Domain", "Posture", "What it does",
                           title=f"Matches for '{search}'")
        for skill in catalog.search(search):
            table.add_row(f"[value]{skill.name}[/]", f"[muted]{skill.domain}[/]",
                          skill.posture, f"[muted]{truncate(skill.description, 52)}[/]")
        console.print(table)
        return

    counts = catalog.posture_counts()
    table = data_table("Domain", {"header": "Skills", "justify": "right"},
                       title=f"{len(catalog)} skills loaded from {catalog.root.name}")
    for domain, n in list(catalog.domains().items())[:16]:
        table.add_row(f"[value]{domain}[/]", str(n))
    console.print(table)

    console.print()
    console.print(f"  [ok]{counts.get(DEFENSIVE, 0)} defensive[/] "
                  f"[muted]{DASH} may be applied automatically[/]")
    console.print(f"  [muted]{counts.get(DUAL_USE, 0)} dual-use {DASH} listed, never automatic[/]")
    console.print(f"  [warn]{counts.get(OFFENSIVE, 0)} offensive[/] "
                  f"[muted]{DASH} listed, never automatic[/]")
    console.print()
    console.print("[muted]Offensive and unlabelled skills stay searchable and readable, but the")
    console.print("orchestrator never reaches for them on its own. Use them deliberately, and")
    console.print("only against systems you are authorised to test.[/]")


def _refresh_models(
    kind: Optional[str] = None, search: Optional[str] = None, free_only: bool = False
) -> None:
    """Ask each connected provider what it serves, and print the answer.

    Deliberately does not write anything back. Discovery reports; choosing
    stays with the operator, because silently repointing a running deployment
    at a model found this morning would be a bad surprise.
    """
    import asyncio

    from aether.config.discovery import discover_all, rank
    from aether.config.providers import is_free, selectable
    from aether.domain.models import ProviderConfig

    candidates = [p for p in selectable() if p.base_url]
    if kind:
        candidates = [p for p in candidates if p.kind == kind]
    if free_only:
        candidates = [p for p in candidates if is_free(p)]
    if search:
        needle = search.lower()
        candidates = [p for p in candidates if needle in p.id.lower() or needle in p.name.lower()]

    configs = [
        ProviderConfig(
            provider_id=p.id, display_name=p.name,
            base_url=p.base_url, api_key_env=p.api_key_env or "",
        )
        for p in candidates
    ]
    by_id = {p.id: p for p in candidates}

    results = []
    if configs:
        console.print(f"[muted]Asking {len(configs)} providers what they serve now…[/]")
        results = asyncio.run(discover_all(configs))

    answered = [r for r in results if r.ok and r.models]
    for res in answered:
        spec = by_id[res.provider_id]
        names = rank(res.models, hint=spec.default_model)
        table = data_table(
            "Model", title=f"{spec.name} {DASH} {res.summary}"
        )
        for name in names[:40]:
            marker = "  [ok]← configured[/]" if name == spec.default_model else ""
            table.add_row(f"[value]{name}[/]{marker}")
        console.print(table)
        if len(names) > 40:
            console.print(f"[muted]  …and {len(names) - 40} more[/]")
        if spec.default_model not in res.models:
            # The reason this command exists: the catalogue default has aged
            # out and would 404 from a config file that looks authoritative.
            console.print(
                f"  [warn]configured default '{spec.default_model}' is not in "
                f"this list[/] [muted]{DASH} set one above with[/] "
                f"[accent]/model {spec.id}:<name>[/]"
            )

    silent = [r for r in results if not r.ok or not r.models]
    if silent:
        console.print()
        console.print("[muted]Not asked or no answer:[/]")
        for res in silent:
            console.print(f"  [muted]{res.provider_id:<22}{res.summary}[/]")

    # CLI providers have no /v1/models, and skipping them silently is what let
    # a guessed model name ("opus-5") reach the help text and get rejected by
    # the CLI. Their own --help is the authority, so it is shown verbatim.
    cli_specs = [p for p in selectable() if p.command and (not kind or p.kind == kind)]
    if search:
        needle = search.lower()
        cli_specs = [p for p in cli_specs if needle in p.id.lower() or needle in p.name.lower()]
    if cli_specs:
        cli_results = asyncio.run(
            _gather_cli(sorted({p.command for p in cli_specs}))
        )
        installed = [(c, r) for c, r in cli_results if r.ok]
        if installed:
            console.print()
            table = data_table("CLI", "What its --model accepts",
                               title="Signed-in CLIs (they run whatever you are logged into)")
            for command, res in installed:
                table.add_row(f"[value]{command}[/]", f"[muted]{truncate(res.models[0], 92)}[/]")
            console.print(table)
            console.print("[muted]Set one with[/] [accent]/model <provider_id>:<name>[/]")

    if not answered:
        console.print()
        console.print("[muted]Connect a provider first:[/] [accent]aether connect <id>[/]")


async def _gather_cli(commands: list[str]):
    """Ask each installed CLI in parallel; a missing binary is just a result."""
    import asyncio as _asyncio

    from aether.config.discovery import discover_cli

    results = await _asyncio.gather(*(discover_cli(c) for c in commands))
    return list(zip(commands, results))


@app.command("models")
def models_cmd(
    kind: Optional[str] = typer.Option(
        None, "--kind", "-k", help="cli | aggregator | api | local"
    ),
    search: Optional[str] = typer.Option(None, "--search", "-s", help="Filter by name or id"),
    ready_only: bool = typer.Option(
        False, "--ready", help="Only providers this machine can already reach"
    ),
    free_only: bool = typer.Option(
        False, "--free", help="Only providers usable without per-token charges"
    ),
    refresh: bool = typer.Option(
        False, "--refresh", help="Ask each connected provider which models it serves now"
    ),
) -> None:
    """Every model provider AETHER can connect to, and which are ready here.

    Reads no database and boots nothing, so it works before `aether setup`.
    """
    from aether.config.providers import (
        COST_LABEL,
        KIND_LABEL,
        KIND_ORDER,
        free_model_for,
        is_free,
        is_ready,
        selectable,
    )

    _load_local_secrets()

    if refresh:
        _refresh_models(kind=kind, search=search, free_only=free_only)
        return

    needle = (search or "").lower()
    shown = 0

    for k in KIND_ORDER:
        if kind and kind != k:
            continue
        group = [p for p in selectable() if p.kind == k]
        if free_only:
            group = [p for p in group if is_free(p)]
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
            "Model",
            "Cost",
            {"header": "Ready", "justify": "center"},
            "How",
            title=KIND_LABEL[k],
        )
        for p, ready, how in rows:
            # With --free, show the model that is actually free rather than a
            # paid default — OpenRouter's default is a paid frontier model, so
            # printing it under a "free" filter would be a billing surprise.
            model = free_model_for(p) if free_only else (p.default_model or "-")
            table.add_row(
                f"[value]{p.id}[/]" if ready else f"[muted]{p.id}[/]",
                p.name,
                truncate(model or "-", 28),
                COST_LABEL.get(p.cost, p.cost),
                check_text(ready),
                f"[muted]{truncate(how, 30)}[/]",
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
    if free_only:
        console.print(
            "[muted]Free tiers change often — these come from vendor docs, not a "
            "live check.\n'free' is local hardware and stays free; 'free tier' is "
            "an allowance that can\nshrink or end. Subscription CLIs are excluded: "
            "no per-token cost, but not free.[/]"
        )
    if not (ready_only or kind or search or free_only):
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
            # Fragments never reach HTTP access logs or referrer headers. The
            # dashboard exchanges this bootstrap value for an HttpOnly cookie.
            dashboard = f"http://{h}:{p}/#token={token}"
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
    demo: bool = typer.Option(False, "--demo", help="Use simulated mock output"),
) -> None:
    """Create and start a task."""

    async def _go() -> None:
        application = await _boot(config, demo=demo)
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
