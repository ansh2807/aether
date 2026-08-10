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
from aether.cli.ui import (
    ARROW,
    BANNER,
    BULLET,
    DASH,
    DOT,
    ELLIPSIS,
    FAIL,
    GTE,
    OK,
    PROMPT,
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


HELP_TEXT = """
[bold cyan]AETHER = LLM router + agent terminal[/]

[bold]Just type[/] - chat with the active model (auto-switch if limits hit;
auto-summary + new session segment when context is full).

[bold]Commands[/]
  [cyan]/help[/]                 This help
  [cyan]/status[/]               Models, session, context usage
  [cyan]/agents[/]               Enabled providers
  [cyan]/model[/] <id>           Switch active model now
  [cyan]/models[/] [cyan]/setup[/]        Re-login / pick Claude / Google / Grok / Kimi / OpenCode
  [cyan]/connect[/] <id>         Connect provider (OAuth / key)
  [cyan]/new[/]                  New chat session (keeps providers)
  [cyan]/sessions[/]             List chat sessions
  [cyan]/resume[/] <id>          Resume a chat session
  [cyan]/compact[/]              Force summarize + rollover now
  [cyan]/debate[/] <topic>       Multi-model debate (models talk)
  [cyan]/task[/] <goal>          Full orchestrator task (plan -> agents -> verify)
  [cyan]/mode[/] chat|debate|task
  [cyan]/tasks[/]                Orchestrator task list
  [cyan]/inspect[/] <TASK-id>    Inspect orchestrator task
  [cyan]/doctor[/]               Diagnostics
  [cyan]/clear[/]                Clear screen
  [cyan]/quit[/]                 Exit

[bold]Examples[/]
  Explain this error: ...
  /debate Best architecture for a QR file transfer app
  /task Build a small Python CLI tool
"""


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
    console.print(
        Panel(
            "[bold]You are in chat mode[/] (like Grok / Claude Code).\n"
            f"{BULLET} One active model answers\n"
            f"{BULLET} If it hits rate/quota/auth/context limits {ARROW} [green]auto-switch[/] to next model\n"
            f"{BULLET} If context fills up {ARROW} [green]auto-summary[/] + new segment, continue where you left off\n"
            f"{BULLET} [cyan]/debate topic[/] for multi-model discussion {DOT} [cyan]/task goal[/] for full orchestration",
            border_style="green",
            title="ready",
        )
    )

    while True:
        console.print()
        try:
            console.print(Rule(style="dim"))
            active = session.active_provider or "?"
            raw = console.input(f"[bold cyan]{PROMPT}[/] [dim]({active})[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Session saved. Bye.[/]")
            app.chat_store.save(session)
            break

        if not raw:
            continue

        if raw in {"/quit", "/exit", ":q", "quit", "exit"}:
            app.chat_store.save(session)
            console.print("[dim]Bye.[/]")
            break

        if raw in {"/help", "help", "?"}:
            console.print(Panel(HELP_TEXT, title="help", border_style="blue"))
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
            console.print(f"[yellow]Unknown command[/] {DASH} /help")
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
