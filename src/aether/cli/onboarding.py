"""Interactive onboarding - mode, providers, keys, model connect.

Runs when the user turns AETHER on (interactive start).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Optional

import yaml
from rich.panel import Panel
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from aether import __version__
from aether.cli.ui import console
from aether.app import AetherApp, get_app
from aether.config.session import (
    OPENCODE_FALLBACK_MODELS,
    UserProfile,
    apply_profile_to_settings_dict,
    catalog_by_id,
    env_path,
    load_dotenv_file,
    load_profile,
    primary_login_providers,
    save_profile,
)
from aether.config.settings import find_config_path, load_yaml_config, settings_from_dict
from aether.domain.enums import TaskMode
from aether.domain.models import ExecutionRequest



def _data_dir() -> Path:
    # Prefer project data/ so everything stays local
    return (Path.cwd() / "data").resolve()


def _default_config_dict() -> dict[str, Any]:
    path = find_config_path()
    if path and path.exists():
        return load_yaml_config(path)
    return {}


def _print_banner() -> None:
    console.print(
        Panel.fit(
            f"[bold cyan]AETHER[/] v{__version__}\n"
            "[dim]Universal Multi-Agent AI Orchestrator[/]\n\n"
            "Login asks:\n"
            "  1. Solo or multi-agent\n"
            f"  2. Platforms: [bold]Claude . Google . Grok . Kimi . Open Code[/]\n"
            "  3. Model(s) per platform (Open Code = multi-model)\n"
            "  4. Browser login / API keys\n"
            "  5. Connect + start",
            border_style="cyan",
            title="Welcome",
        )
    )


def _choose_mode(current: TaskMode) -> TaskMode:
    console.print("\n[bold]1) How should AETHER work?[/]\n")
    console.print("  [green]1[/]  Solo          - one model does the whole task")
    console.print("  [green]2[/]  Multi-agent   - several models collaborate (architect / coder / tester)")
    console.print("  [green]3[/]  Debate        - multiple proposals + critiques")
    console.print("  [green]4[/]  Parallel      - independent workstreams in parallel")
    default = {
        TaskMode.SOLO: "1",
        TaskMode.COLLAB: "2",
        TaskMode.DEBATE: "3",
        TaskMode.PARALLEL: "4",
    }.get(current, "1")
    choice = Prompt.ask("Choose", choices=["1", "2", "3", "4"], default=default)
    return {
        "1": TaskMode.SOLO,
        "2": TaskMode.COLLAB,
        "3": TaskMode.DEBATE,
        "4": TaskMode.PARALLEL,
    }[choice]


def _login_catalog() -> list[dict[str, Any]]:
    """Primary lineup first: Claude, Google, Grok, Kimi, Open Code."""
    return primary_login_providers()


def _list_catalog_table() -> None:
    catalog = _login_catalog()
    table = Table(title="Platforms (pick during login)")
    table.add_column("#", style="cyan", width=3)
    table.add_column("Platform")
    table.add_column("Auth")
    table.add_column("Notes")
    for i, c in enumerate(catalog, start=1):
        connect = c.get("connect") or c["kind"]
        if connect == "cli_oauth":
            auth = "Browser / CLI login"
        elif connect == "browser_key":
            auth = "API key page"
        else:
            auth = "none"
        note = c.get("blurb") or ""
        if c.get("multi_model"):
            note = "MULTI-MODEL hub - " + note
        table.add_row(str(i), c["name"], auth, note[:48])
    console.print(table)


def _choose_providers(mode: TaskMode, previous: list[str]) -> list[str]:
    console.print("\n[bold]2) Which platforms?[/]  [dim]Claude . Google . Grok . Kimi . Open Code[/]\n")
    catalog = _login_catalog()
    _list_catalog_table()

    if mode == TaskMode.SOLO:
        console.print("\n[dim]Solo: pick [bold]one[/bold] platform number.[/]")
        default = "1"
        if previous:
            for i, c in enumerate(catalog, start=1):
                if c["id"] == previous[0]:
                    default = str(i)
                    break
        n = IntPrompt.ask("Platform #", default=int(default))
        n = max(1, min(n, len(catalog)))
        return [catalog[n - 1]["id"]]

    console.print(
        "\n[dim]Multi-agent: numbers separated by commas "
        "(e.g. [bold]1,3,4,5[/bold] = Claude + Grok + Kimi + Open Code).[/]"
    )
    console.print("[dim]Tip: Open Code alone can host many models for multi-agent.[/]")
    default_str = ",".join(
        str(i) for i, c in enumerate(catalog, start=1) if c["id"] in previous
    ) or "1,3,5"
    raw = Prompt.ask("Platform numbers", default=default_str)
    selected: list[str] = []
    for part in raw.replace(" ", "").split(","):
        if not part.isdigit():
            continue
        idx = int(part)
        if 1 <= idx <= len(catalog):
            pid = catalog[idx - 1]["id"]
            if pid not in selected:
                selected.append(pid)
    if not selected:
        console.print("[yellow]Nothing selected - using Mock.[/]")
        return ["mock"]
    return selected


async def _load_opencode_models() -> list[str]:
    try:
        from aether.adapters.opencode_adapter import list_opencode_models

        models = await list_opencode_models()
        if models:
            return models
    except Exception:  # noqa: BLE001
        pass
    return list(OPENCODE_FALLBACK_MODELS)


def _choose_opencode_models(previous: list[str], all_models: list[str]) -> list[str]:
    """Multi-select models from Open Code hub."""
    console.print("\n[bold magenta]Open Code - multi-model choice[/]\n")
    console.print(
        "[dim]Pick one or many models Open Code can run "
        "(Kimi, Grok, Claude, Gemini, GPT, ...).[/]\n"
    )
    # Show curated + live, grouped
    display = all_models[:60]  # cap UI
    for i, m in enumerate(display, start=1):
        mark = " *" if m in previous else ""
        console.print(f"  {i:2}. {m}{mark}")
    if len(all_models) > 60:
        console.print(f"  ... and {len(all_models) - 60} more (type full id if needed)")

    default_idx: list[str] = []
    for m in previous:
        if m in display:
            default_idx.append(str(display.index(m) + 1))
    if not default_idx:
        # sensible multi defaults
        for pref in (
            "opencode/claude-sonnet-4-5",
            "opencode/kimi-k2.7-code",
            "opencode/grok-4.5",
            "opencode/gemini-3.5-flash",
        ):
            if pref in display:
                default_idx.append(str(display.index(pref) + 1))
    default_str = ",".join(default_idx[:4]) if default_idx else "1"

    raw = Prompt.ask(
        "Model numbers (comma) or full ids",
        default=default_str,
    )
    chosen: list[str] = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        if part.isdigit():
            idx = int(part)
            if 1 <= idx <= len(display):
                mid = display[idx - 1]
                if mid not in chosen:
                    chosen.append(mid)
        else:
            # full model id
            if part not in chosen:
                chosen.append(part)
    if not chosen:
        chosen = [display[0]] if display else list(OPENCODE_FALLBACK_MODELS[:1])
    console.print(f"  -> Open Code models: [bold]{', '.join(chosen)}[/]\n")
    return chosen


def _choose_models(
    provider_ids: list[str],
    previous: dict[str, str],
    previous_oc: list[str],
    opencode_model_list: list[str],
) -> tuple[dict[str, str], list[str]]:
    console.print("\n[bold]3) Model selection[/]\n")
    models: dict[str, str] = {}
    opencode_picks: list[str] = []

    for pid in provider_ids:
        cat = catalog_by_id(pid)
        if not cat:
            continue

        # Open Code: multi-model
        if pid == "opencode_cli" or cat.get("multi_model"):
            opencode_picks = _choose_opencode_models(previous_oc, opencode_model_list)
            if opencode_picks:
                models[pid] = opencode_picks[0]
            continue

        options = list(cat.get("models") or [cat.get("default_model", pid)])
        console.print(f"[cyan]{cat['name']}[/]")
        if cat.get("blurb"):
            console.print(f"  [dim]{cat['blurb']}[/]")
        for i, m in enumerate(options, start=1):
            console.print(f"  {i}. {m}")
        default_model = previous.get(pid) or cat.get("default_model") or options[0]
        default_idx = 1
        if default_model in options:
            default_idx = options.index(default_model) + 1

        if cat["kind"] == "mock":
            models[pid] = default_model
            console.print(f"  -> [bold]{default_model}[/]\n")
            continue

        if cat["kind"] == "cli" and pid == "claude_cli":
            # optional model preference label
            choice = IntPrompt.ask("  Prefer which Claude tier? (CLI uses your account)", default=default_idx)
            choice = max(1, min(choice, len(options)))
            models[pid] = options[choice - 1]
            console.print(f"  -> [bold]{models[pid]}[/]\n")
            continue

        if cat["kind"] == "cli":
            models[pid] = default_model
            console.print(f"  -> [bold]{default_model}[/]\n")
            continue

        choice = IntPrompt.ask("  Choose model #", default=default_idx)
        choice = max(1, min(choice, len(options)))
        chosen = options[choice - 1]
        if chosen == "custom":
            chosen = Prompt.ask("  Enter custom model name", default="llama3")
        models[pid] = chosen
        console.print(f"  -> [bold]{chosen}[/]\n")

    return models, opencode_picks


def _collect_keys(provider_ids: list[str], data_dir: Path) -> dict[str, str]:
    """Connect providers like official apps: browser OAuth or key page + paste."""
    from aether.cli.connect import connect_provider

    console.print("\n[bold]4) Connect accounts (browser login / API keys)[/]\n")
    console.print(
        Panel.fit(
            "AETHER connects the [bold]same way the official tools do[/]:\n\n"
            "- [cyan]Claude[/] -> opens browser OAuth via [bold]claude auth login[/]\n"
            "  (same style as claude.com/.../oauth/authorize - Claude Code flow)\n"
            "- [cyan]OpenAI / Grok / Gemini[/] -> opens their key console -> you paste the key\n"
            "- Keys go only to [bold]data/.env[/bold] - never into task memory",
            border_style="cyan",
            title="How auth works",
        )
    )
    env_file = env_path(data_dir)
    load_dotenv_file(env_file)
    collected: dict[str, str] = {}

    for pid in provider_ids:
        cat = catalog_by_id(pid) or {}
        console.print(f"\n-- [bold]{cat.get('name', pid)}[/] --")
        result = connect_provider(pid, data_dir)
        if result.ok:
            console.print(f"[green]+ Connected[/] ({result.method}): {result.message}")
        else:
            console.print(f"[yellow]o Not connected[/] ({result.method}): {result.message}")

        if cat.get("kind") == "api" and cat.get("ask_base_url"):
            base = Prompt.ask(
                "  Base URL for OpenAI-compatible API",
                default=str(cat.get("base_url") or "http://127.0.0.1:11434/v1"),
            )
            collected[f"BASE_URL__{pid}"] = base

        # track env keys that now exist for collected dict
        env_name = cat.get("api_key_env")
        if env_name and os.environ.get(env_name):
            collected[env_name] = os.environ[env_name]

    return collected


def _apply_base_urls(config: dict[str, Any], collected: dict[str, str], models: dict[str, str]) -> dict[str, Any]:
    providers = dict(config.get("providers") or {})
    for key, val in collected.items():
        if key.startswith("BASE_URL__"):
            pid = key.split("__", 1)[1]
            if pid in providers and isinstance(providers[pid], dict):
                providers[pid] = dict(providers[pid])
                providers[pid]["base_url"] = val
    for pid, model in models.items():
        if pid in providers and isinstance(providers[pid], dict):
            providers[pid] = dict(providers[pid])
            providers[pid]["model"] = model
    config["providers"] = providers
    return config


async def _connect_and_test(app: AetherApp, provider_ids: list[str]) -> dict[str, bool]:
    console.print("\n[bold]5) Connecting to selected models...[/]\n")
    results: dict[str, bool] = {}
    health = await app.registry.health_all()
    by_id = {h.provider_id: h for h in health}

    table = Table(title="Connection status")
    table.add_column("Provider")
    table.add_column("Status")
    table.add_column("Detail")

    for pid in provider_ids:
        h = by_id.get(pid)
        if h is None:
            table.add_row(pid, "[red]MISSING[/]", "not registered")
            results[pid] = False
            continue
        ok = h.available
        # For mock, always ok if enabled
        if pid == "mock":
            ok = True
        mark = "[green]ONLINE[/]" if ok else "[red]OFFLINE[/]"
        table.add_row(pid, mark, (h.message or h.availability.value)[:60])
        results[pid] = ok

        # smoke execute for online api/mock
        if ok and pid in app.registry.list_ids():
            adapter = app.registry.get(pid)
            if adapter is None:
                continue
            try:
                req = ExecutionRequest(
                    task_id="CONNECT",
                    provider_id=pid,
                    prompt=(
                        "Reply with a short HANDOFF REPORT only:\n"
                        "STATUS: COMPLETE\nCOMPLETED:\n- connection ok\n"
                        "REMAINING:\n- none\nRECOMMENDED_NEXT_ACTION:\nready\n"
                    ),
                    workspace_path=str(app.settings.workspace_root / "_connect"),
                    timeout_seconds=45,
                )
                (app.settings.workspace_root / "_connect").mkdir(parents=True, exist_ok=True)
                # Skip real API smoke for expensive providers - health is enough unless mock
                if pid == "mock" or catalog_by_id(pid) and catalog_by_id(pid)["kind"] == "mock":
                    result = await adapter.execute(req)
                    if not result.success:
                        results[pid] = False
                        table.add_row(pid, "[yellow]SMOKE FAIL[/]", (result.error or "")[:50])
            except Exception as exc:  # noqa: BLE001
                console.print(f"  [dim]{pid} smoke skipped: {exc}[/]")

    console.print(table)
    online = sum(1 for v in results.values() if v)
    if online == 0:
        console.print(
            "\n[red bold]No providers connected.[/] "
            "You can re-run setup, fix keys, or enable Mock for offline use."
        )
    else:
        console.print(f"\n[green bold]Connected:[/] {online}/{len(provider_ids)} provider(s) ready.")
    return results


def build_app_from_profile(profile: UserProfile, config_path: Optional[str] = None) -> AetherApp:
    """Load base config, apply profile, inject env, return fresh app."""
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    load_dotenv_file(env_path(data_dir))

    base = _default_config_dict()
    if not base:
        # minimal
        base = {"providers": {}, "data_dir": str(data_dir), "workspace_root": str(Path.cwd() / "workspace")}

    merged = apply_profile_to_settings_dict(base, profile)
    # ensure paths
    merged["data_dir"] = str(data_dir)
    if "workspace_root" not in merged:
        merged["workspace_root"] = str(Path.cwd() / "workspace")

    # apply models into provider blocks
    providers = merged.get("providers") or {}
    for pid, model in profile.models.items():
        if pid in providers and isinstance(providers[pid], dict):
            providers[pid]["model"] = model
            providers[pid]["enabled"] = pid in profile.selected_providers
    merged["providers"] = providers

    settings = settings_from_dict(merged)
    # force reload global app (called for the side effect on the module global)
    get_app(force_reload=True)
    # replace settings by constructing new app with our settings
    from aether.app import AetherApp as AppCls
    import aether.app as app_mod

    application = AppCls(settings)
    app_mod._app = application
    return application


async def run_onboarding(
    force: bool = False,
    config: Optional[str] = None,
) -> tuple[AetherApp, UserProfile]:
    """Full turn-on wizard. Returns ready app + profile."""
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    load_dotenv_file(env_path(data_dir))

    profile = load_profile(data_dir)
    _print_banner()

    if profile.setup_complete and profile.selected_providers and not force:
        console.print(
            f"[dim]Last session:[/] mode=[bold]{profile.mode.value}[/]  "
            f"models=[bold]{', '.join(profile.selected_providers)}[/]"
        )
        if Confirm.ask("Use last settings?", default=True):
            app = build_app_from_profile(profile, config)
            await app.start()
            await _connect_and_test(app, profile.selected_providers)
            return app, profile
        console.print("[dim]Reconfiguring...[/]\n")

    # --- wizard steps ---
    mode = _choose_mode(profile.mode)
    providers = _choose_providers(mode, profile.selected_providers)
    oc_list: list[str] = []
    if "opencode_cli" in providers:
        console.print("\n[dim]Loading Open Code model catalog...[/]")
        oc_list = await _load_opencode_models()
        console.print(f"[dim]Found {len(oc_list)} Open Code models[/]")
    models, opencode_picks = _choose_models(
        providers,
        profile.models,
        profile.opencode_models,
        oc_list or list(OPENCODE_FALLBACK_MODELS),
    )
    collected = _collect_keys(providers, data_dir)

    profile = UserProfile(
        mode=mode,
        selected_providers=providers,
        models=models,
        opencode_models=opencode_picks,
        setup_complete=True,
        prefer_cli=True,
    )
    save_profile(data_dir, profile)

    # Build config with base URLs if any
    base = _default_config_dict()
    merged = apply_profile_to_settings_dict(base, profile)
    merged = _apply_base_urls(merged, collected, models)
    merged["data_dir"] = str(data_dir)
    merged.setdefault("workspace_root", str(Path.cwd() / "workspace"))

    # Persist a user config so serve/other commands pick it up
    user_cfg = Path.cwd() / "config" / "aether.yaml"
    user_cfg.parent.mkdir(parents=True, exist_ok=True)
    # Don't write secrets into yaml - only enable flags + models
    safe = apply_profile_to_settings_dict(_default_config_dict(), profile)
    safe = _apply_base_urls(safe, {k: v for k, v in collected.items() if k.startswith("BASE_URL__")}, models)
    safe["data_dir"] = "./data"
    safe["workspace_root"] = "./workspace"
    user_cfg.write_text(yaml.safe_dump(safe, sort_keys=False), encoding="utf-8")
    console.print(f"\n[dim]Saved preferences -> {user_cfg}[/]")
    console.print(f"[dim]Saved secrets    -> {env_path(data_dir)}[/]")

    settings = settings_from_dict(merged)
    from aether.app import AetherApp as AppCls
    import aether.app as app_mod

    application = AppCls(settings)
    app_mod._app = application
    await application.start()
    # Include virtual Open Code providers in health check
    test_ids = list(providers)
    for m in opencode_picks:
        from aether.config.session import safe_provider_id

        vid = safe_provider_id(m)
        if vid not in test_ids:
            test_ids.append(vid)
    await _connect_and_test(application, test_ids)

    console.print("\n[bold]Active lineup[/]")
    for pid in providers:
        mid = models.get(pid, "-")
        console.print(f"  - {pid}: {mid}")
    for m in opencode_picks:
        console.print(f"  - open code -> [magenta]{m}[/]")

    if mode != TaskMode.SOLO and (len(providers) > 1 or len(opencode_picks) > 1):
        console.print(
            "\n[dim]Multi-agent: AETHER routes architect / implementer / tester / reviewer "
            "across these models automatically.[/]"
        )

    console.print(
        Panel.fit(
            f"[green]AETHER is ON[/]\n"
            f"Mode: [bold]{mode.value}[/]\n"
            f"Platforms: [bold]{', '.join(providers)}[/]\n"
            + (
                f"Open Code models: [bold]{', '.join(opencode_picks)}[/]\n"
                if opencode_picks
                else ""
            )
            + "\nType your task goal below.\n"
            "Commands: /setup  /mode  /models  /status  /quit",
            border_style="green",
            title="Ready",
        )
    )
    return application, profile


def reconfigure_menu(app: AetherApp, profile: UserProfile) -> tuple[AetherApp, UserProfile]:
    """Sync helper for /models command."""
    return asyncio.get_event_loop().run_until_complete(run_onboarding(force=True))
