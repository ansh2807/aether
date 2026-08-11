"""Open Code was the last adapter still buffering its output to the end.

`docs/DURABILITY.md` tracked this as the remaining gap in WO-02: the CLI adapter
mirrored output to disk as it arrived, the API adapter was fixed once the
catalogue filled with hosted providers, and Open Code still called
`proc.communicate()` — which returns nothing until the child exits. Killing the
process at minute nine of a ten-minute run threw away all nine.

These tests drive the real adapter against a stand-in `opencode` binary (a
Python script), because the point is the adapter's own plumbing, not the agent
behind it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from aether.adapters.opencode_adapter import OpencodeCliAdapter
from aether.domain.enums import AdapterType
from aether.domain.models import ExecutionRequest, ProviderConfig
from aether.execution_log import ExecutionLog


def _fake_binary(script: str, tmp_path: Path) -> Path:
    """A stand-in `opencode` executable that runs `script` and ignores argv.

    The adapter resolves its binary with `shutil.which("opencode")` and ignores
    `config.command` entirely, so pointing the config at a Python file does
    nothing — an earlier version of this test did exactly that and silently
    drove the real Open Code CLI, making live calls against whatever account
    the machine is logged into. Tests must never do that. Everything here goes
    through a wrapper on disk plus a patched `opencode_bin`.
    """
    body = tmp_path / "fake_body.py"
    body.write_text(script, encoding="utf-8")

    if sys.platform == "win32":
        wrapper = tmp_path / "fake_opencode.bat"
        wrapper.write_text(f'@echo off\r\n"{sys.executable}" -u "{body}" %*\r\n', encoding="utf-8")
    else:
        wrapper = tmp_path / "fake_opencode.sh"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -u "{body}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
    return wrapper


def _adapter(script: str, tmp_path: Path, monkeypatch) -> OpencodeCliAdapter:
    fake = _fake_binary(script, tmp_path)
    monkeypatch.setattr(
        "aether.adapters.opencode_adapter.opencode_bin", lambda: str(fake)
    )
    cfg = ProviderConfig(
        provider_id="opencode_cli",
        display_name="Open Code",
        adapter=AdapterType.CLI,
        enabled=True,
        command="opencode",
        model="opencode/test-model",
    )
    return OpencodeCliAdapter(cfg)


def _request(tmp_path: Path, timeout: int = 30) -> ExecutionRequest:
    """A request against a scratch workspace.

    The stand-in agents sleep 5s, not 30. The fake binary is a wrapper script,
    so killing it leaves the Python grandchild holding the pipe until it exits
    on its own — with a 30s sleep these two tests took 60s and dominated the
    whole suite. Five seconds is still far longer than the assertions need, and
    reverting the fix still fails them.
    """
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ExecutionRequest(
        task_id="T", subtask_id="sub-1", provider_id="opencode_cli",
        prompt="do the thing", workspace_path=str(ws), timeout_seconds=timeout,
    )


@pytest.mark.asyncio
async def test_output_is_on_disk_before_the_process_finishes(tmp_path: Path, monkeypatch):
    """The guarantee. Asserted while the child is still running — checking
    afterwards would pass against `communicate()` too, which also ends with the
    output available."""
    adapter = _adapter(
        "import time\n"
        "print('EARLY OUTPUT', flush=True)\n"
        "time.sleep(5)\n",
        tmp_path,
        monkeypatch,
    )
    request = _request(tmp_path)

    runner = asyncio.create_task(adapter.execute(request))
    exec_log = ExecutionLog(request.workspace_path, "sub-1")
    for _ in range(100):
        await asyncio.sleep(0.1)
        if "EARLY OUTPUT" in exec_log.read():
            break
    captured = exec_log.read()

    await adapter.cancel_all()
    runner.cancel()
    try:
        await runner
    except (asyncio.CancelledError, Exception):
        pass

    assert "EARLY OUTPUT" in captured, (
        "output was still buffered in memory when the process was killed"
    )


@pytest.mark.asyncio
async def test_a_timeout_still_leaves_the_partial_output(tmp_path: Path, monkeypatch):
    """A hung agent is the common case, and the one where salvage pays off."""
    adapter = _adapter(
        "import time\n"
        "print('PROGRESS SO FAR', flush=True)\n"
        "time.sleep(5)\n",
        tmp_path,
        monkeypatch,
    )
    request = _request(tmp_path, timeout=2)

    result = await adapter.execute(request)

    assert not result.success
    on_disk = ExecutionLog(request.workspace_path, "sub-1").read()
    assert "PROGRESS SO FAR" in on_disk, (
        "the timeout discarded everything the agent had already produced"
    )


@pytest.mark.asyncio
async def test_a_normal_run_still_returns_its_full_output(tmp_path: Path, monkeypatch):
    """Streaming must not change what a completed run returns."""
    adapter = _adapter(
        "print('line one')\n"
        "print('line two')\n"
        "print('line three')\n",
        tmp_path,
        monkeypatch,
    )
    result = await adapter.execute(_request(tmp_path))

    assert result.success, result.error
    for line in ("line one", "line two", "line three"):
        assert line in result.output, f"{line!r} missing from {result.output!r}"


@pytest.mark.asyncio
async def test_stderr_is_not_mixed_into_the_salvage_log(tmp_path: Path, monkeypatch):
    """Open Code writes progress chatter to stderr. Folding it into the log
    would corrupt any handoff parsed out of a salvaged run."""
    adapter = _adapter(
        "import sys\n"
        "print('REAL ANSWER', flush=True)\n"
        "print('spinner noise', file=sys.stderr, flush=True)\n",
        tmp_path,
        monkeypatch,
    )
    request = _request(tmp_path)
    await adapter.execute(request)

    on_disk = ExecutionLog(request.workspace_path, "sub-1").read()
    assert "REAL ANSWER" in on_disk
    assert "spinner noise" not in on_disk, "stderr leaked into the execution log"


@pytest.mark.asyncio
async def test_no_subtask_id_means_no_log_and_no_crash(tmp_path: Path, monkeypatch):
    """Ad-hoc calls have nothing to mirror into; that must not fail the run."""
    adapter = _adapter("print('fine')\n", tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    request = ExecutionRequest(
        task_id="T", subtask_id=None, provider_id="opencode_cli",
        prompt="hi", workspace_path=str(ws), timeout_seconds=30,
    )
    result = await adapter.execute(request)
    assert result.success, result.error
    assert "fine" in result.output


@pytest.mark.asyncio
async def test_output_beyond_the_cap_is_truncated_but_never_blocks(tmp_path: Path, monkeypatch):
    """The child must keep being drained past max_output_bytes, or it wedges on
    a full pipe and the timeout fires on a run that was working."""
    adapter = _adapter(
        "for i in range(4000):\n"
        "    print('x' * 200, flush=True)\n"
        "print('THE END', flush=True)\n",
        tmp_path,
        monkeypatch,
    )
    adapter.max_output_bytes = 4096
    result = await adapter.execute(_request(tmp_path, timeout=60))

    assert result.success, result.error
    assert len(result.output) <= 8192, "the cap was not applied"
