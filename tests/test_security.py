"""Security redaction and sandbox tests."""

from pathlib import Path

import pytest

from aether.adapters.cli_adapter import CliAgentAdapter
from aether.config.settings import SecuritySettings
from aether.domain.enums import AdapterType, ApprovalLevel, FailureClass
from aether.domain.models import ExecutionRequest, ExecutionResult, ProviderConfig
from aether.security.redaction import SecretRedactor
from aether.security.sandbox import ApprovalRequired, ExecutionSandbox, SecurityError


def test_redact_api_key():
    r = SecretRedactor()
    text = "Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz123456"
    out = r.redact(text)
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in out or "REDACTED" in out


def test_sandbox_denylist():
    s = ExecutionSandbox(SecuritySettings(command_denylist=["rm -rf /"], approval_level=ApprovalLevel.SAFE))
    with pytest.raises(SecurityError):
        s.check_command("rm -rf /")


def test_sandbox_approval_for_install():
    s = ExecutionSandbox(SecuritySettings(approval_level=ApprovalLevel.SAFE))
    with pytest.raises(ApprovalRequired):
        s.check_command("pip install evil-package")
    s.grant("install")
    s.check_command("pip install evil-package")  # should not raise for 'install' substring grant


def test_workspace_escape(tmp_path: Path):
    s = ExecutionSandbox(SecuritySettings(workspace_only=True))
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(SecurityError):
        s.resolve_path(ws, "../outside.txt")


def test_check_workspace_rejects_cwd_outside_root(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    s = ExecutionSandbox(SecuritySettings(workspace_only=True), workspace_root=root)
    assert s.check_workspace(root / "TASK-1") == (root / "TASK-1").resolve()
    with pytest.raises(SecurityError):
        s.check_workspace(tmp_path / "elsewhere")


def test_network_command_gate_is_case_insensitive():
    s = ExecutionSandbox(
        SecuritySettings(
            approval_level=ApprovalLevel.SAFE,
            command_allowlist_prefixes=["git ", "python "],
        )
    )
    with pytest.raises(ApprovalRequired):
        s.check_command("Invoke-WebRequest https://example.com")


# --- sandbox is actually enforced on the execution path ---


async def _run_cli(cfg: ProviderConfig, sandbox: ExecutionSandbox, workspace: str) -> ExecutionResult:
    adapter = CliAgentAdapter(cfg, sandbox=sandbox)
    return await adapter.execute(
        ExecutionRequest(
            task_id="T",
            provider_id=cfg.provider_id,
            prompt="hello",
            workspace_path=workspace,
        )
    )


@pytest.mark.asyncio
async def test_cli_adapter_blocks_denylisted_command(tmp_path: Path):
    root = tmp_path / "workspace"
    ws = root / "TASK-1"
    ws.mkdir(parents=True)
    sandbox = ExecutionSandbox(
        SecuritySettings(command_denylist=["shutdown"], approval_level=ApprovalLevel.AUTO),
        workspace_root=root,
    )
    cfg = ProviderConfig(
        provider_id="danger",
        display_name="Danger",
        adapter=AdapterType.CLI,
        enabled=True,
        command="shutdown",
        args=["-h", "now"],
    )
    result = await _run_cli(cfg, sandbox, str(ws))
    assert not result.success
    assert result.failure_class == FailureClass.POLICY_DENIED
    assert "security policy" in (result.error or "")


@pytest.mark.asyncio
async def test_cli_adapter_blocks_workspace_escape(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    sandbox = ExecutionSandbox(
        SecuritySettings(workspace_only=True, approval_level=ApprovalLevel.AUTO),
        workspace_root=root,
    )
    cfg = ProviderConfig(
        provider_id="git_agent",
        display_name="Git agent",
        adapter=AdapterType.CLI,
        enabled=True,
        command="git",
    )
    result = await _run_cli(cfg, sandbox, str(outside))
    assert not result.success
    assert result.failure_class == FailureClass.POLICY_DENIED


@pytest.mark.asyncio
async def test_cli_adapter_policy_ignores_prompt_text(tmp_path: Path):
    """The prompt is agent input, not a command AETHER chose to run."""
    root = tmp_path / "workspace"
    ws = root / "TASK-1"
    ws.mkdir(parents=True)
    sandbox = ExecutionSandbox(
        SecuritySettings(approval_level=ApprovalLevel.SAFE),
        workspace_root=root,
    )
    cfg = ProviderConfig(
        provider_id="missing_cli",
        display_name="Missing",
        adapter=AdapterType.CLI,
        enabled=True,
        command="this-cli-does-not-exist-aether-xyz",
    )
    adapter = CliAgentAdapter(cfg, sandbox=sandbox)
    result = await adapter.execute(
        ExecutionRequest(
            task_id="T",
            provider_id=cfg.provider_id,
            # would trip the 'delete'/'install' DANGEROUS_OPS substrings if the
            # prompt were treated as a command
            prompt="please delete the stale files and install nothing",
            workspace_path=str(ws),
        )
    )
    assert result.failure_class == FailureClass.CRASH  # missing binary, not policy
    assert result.failure_class != FailureClass.POLICY_DENIED


@pytest.mark.asyncio
async def test_opencode_adapter_receives_sandbox_from_registry(tmp_path: Path):
    """oc_* virtual providers must be built with the sandbox attached."""
    from aether.adapters.opencode_adapter import OpencodeCliAdapter
    from aether.adapters.registry import AgentRegistry
    from aether.config.settings import AetherSettings

    settings = AetherSettings(workspace_root=tmp_path / "workspace")
    settings.providers = {
        "oc_test": ProviderConfig(
            provider_id="oc_test",
            display_name="Open Code · test",
            adapter=AdapterType.CLI,
            enabled=True,
            command="opencode",
            model="opencode/test-model",
            extra={"adapter_class": "aether.adapters.opencode_adapter:OpencodeCliAdapter"},
        )
    }
    sandbox = ExecutionSandbox(settings.security, workspace_root=settings.workspace_root)
    registry = AgentRegistry(settings, sandbox=sandbox)
    registry.build_from_config()
    adapter = registry.get("oc_test")
    assert isinstance(adapter, OpencodeCliAdapter)
    assert adapter.sandbox is sandbox
