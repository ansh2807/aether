"""Top-level `--config` must reach subcommands (per-command -c still wins)."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

import aether.app as app_mod
from aether.cli import main as cli_main
from aether.config.settings import AetherSettings, ServerSettings, find_config_path

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


def test_environment_overrides_yaml_values(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AETHER_DATA_DIR", str(tmp_path / "from-env"))
    monkeypatch.setenv("AETHER_SERVER__PORT", "8123")
    settings = AetherSettings(data_dir=tmp_path / "from-yaml")
    assert settings.data_dir == tmp_path / "from-env"
    assert settings.server.port == 8123


def test_missing_environment_config_is_an_error(monkeypatch, tmp_path: Path):
    missing = tmp_path / "missing.yaml"
    monkeypatch.setenv("AETHER_CONFIG", str(missing))
    with pytest.raises(FileNotFoundError, match="AETHER_CONFIG"):
        find_config_path()


def test_unauthenticated_server_cannot_bind_publicly():
    with pytest.raises(ValidationError, match="loopback"):
        ServerSettings(host="0.0.0.0", require_auth=False)


def test_wildcard_cors_origin_is_rejected():
    with pytest.raises(ValidationError, match="cannot contain"):
        ServerSettings(cors_origins=["*"])


def test_demo_flag_explicitly_enables_simulation(tmp_path: Path):
    cfg = _write_config(tmp_path / "demo", "aether.yaml")
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    data["providers"]["mock"]["enabled"] = False
    cfg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    _reset_cli_state()
    try:
        result = runner.invoke(
            cli_main.app,
            ["--config", str(cfg), "run", "--demo", "Build a demo hello module"],
        )
        assert result.exit_code == 0, result.output
        assert "DEMO MODE" in result.output
        assert "COMPLETED" in result.output
    finally:
        _reset_cli_state()
