"""Top-level `--config` must reach subcommands (per-command -c still wins)."""

from pathlib import Path

import yaml
from typer.testing import CliRunner

import aether.app as app_mod
from aether.cli import main as cli_main

runner = CliRunner()


def _write_config(root: Path, name: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    data = {
        "data_dir": str(root / "data"),
        "workspace_root": str(root / "workspace"),
        "database_url": "sqlite+aiosqlite:///" + (root / "data" / "t.db").as_posix(),
        "providers": {
            "mock": {
                "display_name": "Mock",
                "adapter": "mock",
                "enabled": True,
                "for_testing": True,
                "capabilities": {"coding": 8, "reasoning": 8},
            }
        },
    }
    path = root / name
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _reset_cli_state() -> None:
    cli_main._global_config = None
    app_mod._app = None


def test_global_config_option_reaches_subcommand(tmp_path: Path):
    cfg = _write_config(tmp_path / "global", "aether.yaml")
    _reset_cli_state()
    try:
        result = runner.invoke(cli_main.app, ["--config", str(cfg), "task", "list"])
        assert result.exit_code == 0, result.output
        # the subcommand booted against the config we passed, not cwd defaults
        assert app_mod._app is not None
        assert app_mod._app.settings.data_dir == (tmp_path / "global" / "data")
    finally:
        _reset_cli_state()


def test_per_command_config_wins_over_global(tmp_path: Path):
    global_cfg = _write_config(tmp_path / "global", "aether.yaml")
    local_cfg = _write_config(tmp_path / "local", "aether.yaml")
    _reset_cli_state()
    try:
        result = runner.invoke(
            cli_main.app,
            ["--config", str(global_cfg), "task", "list", "--config", str(local_cfg)],
        )
        assert result.exit_code == 0, result.output
        assert app_mod._app is not None
        assert app_mod._app.settings.data_dir == (tmp_path / "local" / "data")
    finally:
        _reset_cli_state()
