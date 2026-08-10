"""AETHER interactive terminal - LLM router agent session (Grok/Claude-Code style).

Modes:
  chat   - single active model; auto-switch on limits; auto-summarize+rollover on context
  debate - multiple models talk in structured rounds
  task   - full multi-step orchestrator DAG (build/fix workflows)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.align import Align
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from aether import __version__
from aether.app import AetherApp
from aether.cli.commands import CATEGORY_ORDER, COMMANDS, suggest
from aether.cli.prompt import TerminalPrompt
from aether.cli.ui import (
    ARROW,
    BANNER,
    DASH,
    DOT,
    ELLIPSIS,
    FAIL,
    GTE,
    OK,
    console,
    data_table,
    health_text,
    truncate,
)
from aether.config.session import UserProfile
from aether.core.chat_session import ChatSession
from aether.core.context_budget import ContextBudget, context_window_for
from aether.domain.enums import TaskMode
from aether.domain.models import Task


def render_help():
    """The help centre: every command, grouped, generated from the registry."""
    from rich.console import Group

    blocks = []
    for category in CATEGORY_ORDER:
        commands = [c for c in COMMANDS if c.category == category]
        if not commands:
            continue
        table = data_table(
            {"header": "Command", "no_wrap": True, "style": "cyan"},
            {"header": "What it does", "no_wrap": True, "overflow": "ellipsis"},
            title=category,
        )
        for cmd in commands:
            table.add_row(cmd.display, cmd.summary)
        blocks.append(table)

    examples = [c for c in COMMANDS if c.example]
    tips = data_table(
        {"header": "Try", "no_wrap": True, "style": "green"},
        title="Examples",
    )
    for cmd in examples[:4]:
        tips.add_row(cmd.example)
    tips.add_row("What does this error mean? <paste it>   [dim](plain text = just chat)[/]")
    blocks.append(tips)

    return Panel(
        Group(*blocks),
        title="AETHER help",
        subtitle="[dim]type / to filter commands as you type[/]",
        border_style="blue",
        padding=(0, 1),
    )


def render_getting_started(active: str, has_real_provider: bool) -> Panel:
    """First thing a new user reads. Tells them what to type, right now."""
    lines = [
        "[bold]Type a message[/] and the active model answers. That's it.",
        "",
        f"[bold cyan]/[/]        {DOT} lists every command as you type",
        f"[bold cyan]/help[/]    {DOT} the full help centre",
        f"[bold cyan]/task[/]    {DOT} hand a goal to the orchestrator (plan {ARROW} agents {ARROW} verify)",
        f"[bold cyan]/quit[/]    {DOT} save and leave",
    ]
    if not has_real_provider:
        lines += [
            "",
            f"[yellow]You are on the [bold]{active}[/bold] agent.[/] It is a built-in stand-in that makes",
            "up plausible answers so you can try AETHER with no accounts. It is not a",
            "real model, and its replies mean nothing.",
            "",
            f"  [bold cyan]/setup[/]            {DOT} pick and sign in to real models",
            f"  [bold cyan]/connect claude_cli[/] {DOT} or connect just one",
        ]
    return Panel(
        "\n".join(lines),
        title="start here",
        border_style="green" if has_real_provider else "yellow",
        padding=(0, 2),
    )


def _enabled_line(app: AetherApp, profile: UserProfile) -> str:
    parts: list[str] = []
    for cfg in app.registry.list_configs():
        if not (cfg.enabled or (cfg.for_testing and cfg.provider_id == "mock")):
            continue
        model = cfg.model or profile.models.get(cfg.provider_id) or "-"
        short = cfg.provider_id.replace("_cli", "")
        if cfg.provider_id.startswith("oc_"):
            parts.append(f"oc:{(model or short).split('/')[-1]}")
        else:
            parts.append(f"{short}" + (f":{model}" if model and model != "-" else ""))
    return f" {DOT} ".join(parts[:8]) if parts else f"(no models {DASH} /setup)"


def render_header(
    app: AetherApp,
    profile: UserProfile,
    mode: str,
    session: Optional[ChatSession],
) -> Panel:
    models = _enabled_line(app, profile)
    active = session.active_provider if session else "-"
    seg = session.ensure_segment().index if session else 0
    usage_txt = ""
    if session and active:
        b = ContextBudget(context_window=context_window_for(active))
        u = b.usage(session.messages_as_dicts(), summary=session.memory_summary)
        usage_txt = f"  ctx={u['used_tokens']}/{u['context_window']} ({u['ratio']:.0%})"
    left = Text.from_markup(
        f"[bold cyan]AETHER[/] [dim]v{__version__}[/]  "
        f"[dim]|[/]  mode=[bold]{mode}[/]  model=[bold green]{active}[/]  "
        f"seg=[bold]{seg}[/]{usage_txt}\n"
        f"[dim]pool[/] {models}"
    )
    right = Text.from_markup(
        f"[dim]{Path.cwd()}[/]\n"
        f"[dim]{datetime.now().strftime('%H:%M:%S')}[/]\n"
        f"[dim]{(session.session_id if session else '')[:16]}[/]"
    )
    grid = Table.grid(expand=True)
    grid.add_column(ratio=3)
    grid.add_column(justify="right")
    grid.add_row(left, right)
    return Panel(
        grid,
        border_style="cyan",
        title="[bold]llm-router session[/]",
        subtitle=f"[dim]chat {DOT} auto-failover {DOT} auto-compact {DOT} /debate {DOT} /task[/]",
    )


def render_banner() -> None:
    console.print(Align.center(Text(BANNER, style="bold cyan")))
    console.print(
        Align.center(
            Text.from_markup(
                f"[dim]LLM Router Terminal[/] {DASH} single model {DOT} auto-switch {DOT} multi-model debate {DOT} session memory"
            )
        )
    )
    console.print()


async def _print_status(app: AetherApp, session: Optional[ChatSession], mode: str) -> None:
    health = await app.registry.health_all()
    table = data_table(
        {"header": "Provider", "no_wrap": True},
        {"header": "State", "no_wrap": True},
        {"header": "Detail", "no_wrap": True, "overflow": "ellipsis"},
        title="Provider pool",
    )
    for h in health:
        if h.availability.value == "DISABLED" and not h.available:
            continue
        table.add_row(h.provider_id, health_text(h.available), f"[dim]{truncate(h.message, 48)}[/]")
    console.print(table)
    if session:
        active = session.active_provider or "-"
        b = ContextBudget(context_window=context_window_for(active))
        u = b.usage(session.messages_as_dicts(), summary=session.memory_summary)
        console.print(
            f"  session={session.session_id}  mode={mode}  active={active}\n"
            f"  segment={session.ensure_segment().index}  "
            f"messages={len(session.current_messages())}  "
            f"context={u['used_tokens']}/{u['context_window']} ({u['ratio']:.0%})  "
            f"compact@{GTE}{u['soft_limit']}"
        )
        if session.memory_summary:
            console.print(Panel(session.memory_summary[:1200], title="memory summary", border_style="dim"))


async def _run_orchestrator_task(app: AetherApp, prompt: str, task_mode: TaskMode) -> None:
    from rich.prompt import Confirm

    preview = Task(original_prompt=prompt, mode=task_mode)
    plan = app.workflow.plan(preview)
    plan_table = data_table(
        {"header": "#", "width": 3, "justify": "right"},
        "Subtask",
        "Role",
        "Depends on",
        title=f"Plan {DOT} {task_mode.value}",
    )
    titles = {s.subtask_id: s.title for s in plan}
    for i, s in enumerate(plan, start=1):
        deps = ", ".join(titles.get(d, d) for d in s.depends_on)
        plan_table.add_row(
            str(i), s.title, f"[accent]{s.role.value}[/]", f"[dim]{deps or DASH}[/]"
        )
    console.print()
    console.print(plan_table)
    if not Confirm.ask("[bold]Run this plan?[/]", default=True):
        console.print(f"[dim]{DASH} cancelled[/]")
        return

    task = await app.orchestrator.submit(prompt, mode=task_mode, auto_start=True)
    console.print(
        Panel(
            f"[bold]{truncate(prompt, 68)}[/]\n[dim]{task.task_id}[/]",
            title="task started",
            border_style="cyan",
            padding=(0, 2),
        )
    )

    from aether.cli.main import _follow_task

    t = await _follow_task(app, task.task_id)
    if t is None:
        return
    if t.final_output:
        console.print(
            Panel(Markdown(t.final_output[:4000]), title="result", border_style="green")
        )
    elif t.failure_reason:
        console.print(Panel(t.failure_reason, title="failed", border_style="red"))


async def run_terminal(
    app: AetherApp,
    profile: UserProfile,
    *,
    config: Optional[str] = None,
) -> None:
    data_dir = Path.cwd() / "data"
    mode = "chat"  # chat | debate | task
    task_mode = profile.mode if profile.setup_complete else TaskMode.SOLO

    preferred = (
        app.settings.orchestrator.default_chat_provider
        or (profile.selected_providers[0] if profile.selected_providers else None)
    )
    session = app.chat_router.create_session(
        provider_id=preferred,
        mode="chat",
        title="AETHER terminal",
    )

    console.clear()
    render_banner()
    console.print(render_header(app, profile, mode, session))
    has_real = any(
        cfg.enabled and not cfg.for_testing for cfg in app.registry.list_configs()
    )
    console.print(
        render_getting_started(session.active_provider or "mock", has_real)
    )

    prompt = TerminalPrompt()
    if not prompt.menu_available:
        console.print(
            "[dim]Tip: type [/][cyan]/help[/][dim] to see every command.[/]"
        )

    while True:
        console.print()
        try:
            console.print(Rule(style="dim"))
            active = session.active_provider or "?"
            raw = prompt.ask(active).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Session saved. Bye.[/]")
            app.chat_store.save(session)
            break

        if not raw:
            continue

        # a lone "/" means "show me what there is"
        if raw == "/":
            console.print(render_help())
            continue

        if raw in {"/quit", "/exit", ":q", "quit", "exit"}:
            app.chat_store.save(session)
            console.print("[dim]Bye.[/]")
            break

        if raw in {"/help", "/?", "help", "?"}:
            console.print(render_help())
            continue

        if raw in {"/clear", "clear"}:
            console.clear()
            render_banner()
            console.print(render_header(app, profile, mode, session))
            continue

        if raw == "/status":
            await _print_status(app, session, mode)
            continue

        if raw == "/agents":
            table = Table(title="Agents")
            table.add_column("ID")
            table.add_column("Model")
            table.add_column("On")
            for cfg in app.registry.list_configs():
                if cfg.enabled or cfg.for_testing:
                    table.add_row(cfg.provider_id, (cfg.model or "-")[:36], str(cfg.enabled))
            console.print(table)
            continue

        if raw == "/sessions":
            for item in app.chat_store.list_recent(15):
                console.print(
                    f"  {item.get('session_id')}  {item.get('active_provider')}  "
                    f"seg={item.get('segments')}  {item.get('updated_at')}"
                )
            continue

        if raw.startswith("/resume"):
            parts = raw.split(maxsplit=1)
            if len(parts) < 2:
                console.print("Usage: /resume <session_id>")
                continue
            loaded = app.chat_store.load(parts[1].strip())
            if not loaded:
                console.print("[red]Session not found[/]")
                continue
            session = loaded
            mode = session.mode if session.mode in {"chat", "debate"} else "chat"
            console.print(f"[green]Resumed[/] {session.session_id} model={session.active_provider}")
            console.print(render_header(app, profile, mode, session))
            continue

        if raw == "/new":
            session = app.chat_router.create_session(
                provider_id=session.active_provider,
                mode="chat",
                title="AETHER terminal",
            )
            mode = "chat"
            console.print(f"[green]New session[/] {session.session_id}")
            console.print(render_header(app, profile, mode, session))
            continue

        if raw == "/compact":
            ok = await app.chat_router.maybe_compact(session, force=True, reason="user_compact")
            console.print(
                "[green]Compacted + rolled to new segment[/]"
                if ok
                else "[yellow]Nothing to compact[/]"
            )
            console.print(render_header(app, profile, mode, session))
            continue

        if raw.startswith("/model"):
            parts = raw.split(maxsplit=1)
            if len(parts) < 2:
                console.print("Usage: /model <provider_id>")
                console.print(f"  active: {session.active_provider}")
                continue
            pid = parts[1].strip()
            if not app.registry.get(pid):
                console.print(f"[red]Unknown or unloaded provider:[/] {pid}")
                continue
            prev = session.active_provider
            session.active_provider = pid
            if pid not in session.participating_providers:
                session.participating_providers.append(pid)
            app.chat_store.save(session)
            console.print(f"[green]Model[/] {prev} {ARROW} [bold]{pid}[/]")
            continue

        if raw.startswith("/connect"):
            parts = raw.split()
            pid = parts[1] if len(parts) > 1 else "claude_cli"
            from aether.cli.connect import connect_provider

            result = connect_provider(pid, data_dir)
            mark = f"[ok]{OK}[/]" if result.ok else f"[err]{FAIL}[/]"
            console.print(f"{mark} {result.message}")
            continue

        if raw in {"/setup", "/models", "/reconfigure"}:
            from aether.cli.onboarding import run_onboarding

            app, profile = await run_onboarding(force=True, config=config)
            # rebuild chat router bindings already on app
            preferred = profile.selected_providers[0] if profile.selected_providers else session.active_provider
            session = app.chat_router.create_session(provider_id=preferred, mode="chat")
            mode = "chat"
            console.clear()
            render_banner()
            console.print(render_header(app, profile, mode, session))
            continue

        if raw.startswith("/mode"):
            parts = raw.split()
            if len(parts) < 2 or parts[1] not in {"chat", "debate", "task"}:
                console.print("Usage: /mode chat|debate|task")
                continue
            mode = parts[1]
            session.mode = mode if mode != "task" else "chat"
            console.print(f"[green]Mode {ARROW}[/] {mode}")
            continue

        if raw == "/doctor":
            from aether.core.doctor import run_doctor

            report = await run_doctor(app)
            s = report["summary"]
            console.print(f"healthy={s['healthy']} {s['passed']}/{s['checks']}")
            continue

        if raw == "/tasks":
            for t in await app.tasks.list(limit=12):
                console.print(f"  {t.task_id} {t.status.value} {t.progress:.0%} {(t.normalized_goal or '')[:40]}")
            continue

        if raw.startswith("/inspect"):
            parts = raw.split(maxsplit=1)
            if len(parts) < 2:
                console.print(f"Usage: /inspect TASK-{ELLIPSIS}")
                continue
            try:
                data = await app.orchestrator.inspect(parts[1].strip())
                console.print_json(data=data["task"])
            except KeyError:
                console.print("[red]Not found[/]")
            continue

        # --- debate ---
        if raw.startswith("/debate"):
            topic = raw[len("/debate") :].strip()
            if not topic:
                console.print("Usage: /debate <topic>")
                continue
            mode = "debate"
            console.print(Panel(f"[bold]Debate[/]\n{topic}", border_style="magenta"))
            with console.status(f"[magenta]Models debating{ELLIPSIS}[/]"):
                result = await app.debate_engine.run(topic, session=session)
            if not result.get("ok"):
                console.print(f"[red]{result.get('error')}[/]")
                continue
            session = result["session"]
            for turn in result.get("transcript") or []:
                console.print(
                    Panel(
                        (turn.get("text") or "")[:2000],
                        title=f"R{turn.get('round')} {DOT} {turn.get('agent')}",
                        border_style="magenta",
                    )
                )
            if result.get("synthesis"):
                console.print(
                    Panel(
                        Markdown(str(result["synthesis"])[:4000]),
                        title="consensus",
                        border_style="green",
                    )
                )
            for ev in result.get("events") or []:
                console.print(f"  [dim]event:[/] {ev}")
            console.print(render_header(app, profile, mode, session))
            continue

        # --- full orchestrator task ---
        if raw.startswith("/task"):
            goal = raw[len("/task") :].strip()
            if not goal:
                console.print("Usage: /task <goal>")
                continue
            mode = "task"
            await _run_orchestrator_task(app, goal, task_mode)
            continue

        if raw.startswith("/collab "):
            mode = "task"
            await _run_orchestrator_task(app, raw[len("/collab ") :], TaskMode.COLLAB)
            continue
        if raw.startswith("/parallel "):
            mode = "task"
            await _run_orchestrator_task(app, raw[len("/parallel ") :], TaskMode.PARALLEL)
            continue

        if raw.startswith("/"):
            close = suggest(raw)
            if close:
                options = "  ".join(f"[cyan]{c.display}[/]" for c in close)
                console.print(
                    f"[yellow]No such command[/] [bold]{raw.split()[0]}[/]. Did you mean: {options}"
                )
            else:
                console.print(
                    f"[yellow]No such command[/] [bold]{raw.split()[0]}[/]. "
                    f"Type [cyan]/[/] or [cyan]/help[/] to see them all."
                )
            continue

        # --- default: CHAT with live token stream ---
        mode = "chat"
        console.print(f"[dim]{ARROW} streaming {session.active_provider}{ELLIPSIS}[/]\n")
        provider = session.active_provider or "?"
        buffer: list[str] = []
        final: dict = {}
        # live print tokens without markdown until done
        async for ev in app.chat_router.chat_stream(
            session,
            raw,
            max_failovers=app.settings.orchestrator.chat_max_failovers,
        ):
            et = ev.get("type")
            if et == "token":
                piece = ev.get("text") or ""
                buffer.append(piece)
                console.print(piece, end="", highlight=False)
            elif et == "event":
                name = str(ev.get("event") or "")
                if "compact" in name:
                    console.print(
                        f"\n  [yellow]{ARROW} {name}[/] {DASH} summary saved, new segment"
                    )
                elif "switch" in name or "failover" in name:
                    console.print(f"\n  [yellow]{ARROW} {name}[/]")
                    if ev.get("provider"):
                        provider = ev["provider"]
            elif et == "status":
                provider = ev.get("provider") or provider
            elif et == "done":
                final = ev
                console.print()  # newline after stream
            elif et == "error":
                console.print(f"\n[red]{ev.get('error')}[/]")
                final = {"ok": False, "error": ev.get("error")}

        # reload session from store (stream path saves it)
        reloaded = app.chat_store.load(session.session_id)
        if reloaded:
            session = reloaded

        if final.get("ok"):
            reply = final.get("reply") or "".join(buffer)
            # pretty panel for final (in case stream was plain)
            if reply and len(buffer) < 3:
                console.print(
                    Panel(Markdown(reply[:6000]), title=f"[green]{provider}[/]", border_style="green")
                )
            else:
                console.print(f"  [dim]{DASH} {provider} {DOT} stream complete {DASH}[/]")
            usage = final.get("usage") or {}
            if usage:
                console.print(
                    f"  [dim]ctx {usage.get('used_tokens')}/{usage.get('context_window')} "
                    f"({usage.get('ratio', 0):.0%})[/]"
                )
        elif final:
            console.print(
                Panel(str(final.get("error") or "failed"), title="[red]failed[/]", border_style="red")
            )

        console.print(render_header(app, profile, mode, session))
