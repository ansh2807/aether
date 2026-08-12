"""WO-13: the verification gate must not hand its secrets to the code it verifies.

Verification exists because the agent's word is not evidence. Running that same
untrusted code with the operator's full environment — every provider API key on
the machine — undoes the point of the exercise.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import json
import os
from pathlib import Path

import pytest

from aether.config.settings import SecuritySettings
from aether.core.verification import CONTAINER_IMAGE, VerificationEngine

SECRET = "sk-durability-canary-do-not-leak"
PEEK = (
    "import json, os, pathlib\n"
    "pathlib.Path('seen.json').write_text(json.dumps(dict(os.environ)))\n"
)


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "peek.py").write_text(PEEK, encoding="utf-8")
    return ws


def _linux_container_runtime_available() -> bool:
    runtime = shutil.which("docker") or shutil.which("podman")
    if runtime is None:
        return False
    template = "{{.OSType}}" if Path(runtime).name.lower().startswith("docker") else "{{.Host.Os}}"
    try:
        result = subprocess.run(
            [runtime, "info", "--format", template],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip().lower() == "linux"


def _container_image_available(image: str) -> bool:
    runtime = shutil.which("docker") or shutil.which("podman")
    if runtime is None:
        return False
    try:
        result = subprocess.run(
            [runtime, "image", "inspect", image],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@pytest.mark.asyncio
async def test_secrets_are_not_visible_to_verified_code(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    ws = _workspace(tmp_path)

    engine = VerificationEngine(repo=None, sandbox_mode="subprocess")
    code, _ = await engine._run([sys.executable, "peek.py"], cwd=ws, timeout=60)
    assert code == 0

    seen = (ws / "seen.json").read_text(encoding="utf-8")
    assert SECRET not in seen, "agent-written code could read the provider keys"
    assert "OPENAI_API_KEY" not in seen
    assert "ANTHROPIC_API_KEY" not in seen


@pytest.mark.asyncio
async def test_scrubbed_env_still_lets_a_test_run(tmp_path: Path):
    """Isolation is worthless if it breaks the thing it is protecting."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "works.py").write_text(
        "import os, sys, pathlib\n"
        "assert os.environ['AETHER_VERIFICATION'] == '1'\n"
        "pathlib.Path('ran.txt').write_text('ok')\n",
        encoding="utf-8",
    )
    engine = VerificationEngine(repo=None, sandbox_mode="subprocess")
    code, out = await engine._run([sys.executable, "works.py"], cwd=ws, timeout=60)
    assert code == 0, out
    assert (ws / "ran.txt").read_text(encoding="utf-8") == "ok"


@pytest.mark.asyncio
async def test_off_mode_is_documented_as_leaky(tmp_path: Path, monkeypatch):
    """`off` is an explicit trust decision — prove it really does inherit."""
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    ws = _workspace(tmp_path)

    engine = VerificationEngine(repo=None, sandbox_mode="off")
    await engine._run([sys.executable, "peek.py"], cwd=ws, timeout=60)

    seen = (ws / "seen.json").read_text(encoding="utf-8")
    assert SECRET in seen, (
        "'off' no longer inherits the environment — update the docs, the trade-off changed"
    )


@pytest.mark.asyncio
async def test_container_mode_fails_closed_without_a_runtime(tmp_path: Path, monkeypatch):
    """Requested isolation must never silently execute on the host."""
    monkeypatch.setattr(VerificationEngine, "_container_runtime", lambda self: None)
    ws = _workspace(tmp_path)

    engine = VerificationEngine(repo=None, sandbox_mode="container")
    code, out = await engine._run([sys.executable, "peek.py"], cwd=ws, timeout=60)

    assert code != 0
    assert "verification blocked" in out
    assert not (ws / "seen.json").exists(), "untrusted code still ran on the host"


@pytest.mark.asyncio
async def test_container_fallback_requires_explicit_opt_in(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(VerificationEngine, "_container_runtime", lambda self: None)
    ws = _workspace(tmp_path)

    engine = VerificationEngine(
        repo=None, sandbox_mode="container", allow_container_fallback=True
    )
    code, out = await engine._run([sys.executable, "peek.py"], cwd=ws, timeout=60)

    assert code == 0
    assert "explicit fallback enabled" in out
    seen = (ws / "seen.json").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY" not in seen


def test_container_command_uses_hardening_flags(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(VerificationEngine, "_container_runtime", lambda self: "docker")
    engine = VerificationEngine(repo=None, container_image="trusted-verifier:test")

    wrapped = engine._containerise(["python", "probe.py"], tmp_path)

    assert wrapped is not None
    argv, name = wrapped
    assert name.startswith("aether-verify-")
    assert ["--cap-drop", "ALL"] == argv[argv.index("--cap-drop") : argv.index("--cap-drop") + 2]
    assert "no-new-privileges=true" in argv
    assert "--read-only" in argv
    assert "--network" in argv and "none" in argv
    assert "--user" in argv
    assert "trusted-verifier:test" in argv


def test_config_and_engine_use_the_same_pinned_verifier_image():
    assert SecuritySettings().verification_image == CONTAINER_IMAGE
    assert CONTAINER_IMAGE == "aether:local"


@pytest.mark.skipif(
    not _linux_container_runtime_available(),
    reason="no running Linux container engine on this machine",
)
@pytest.mark.asyncio
async def test_container_mode_is_hardened(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "probe.py").write_text(
        "import json, os, socket, pathlib\n"
        "network = 'reachable'\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "except OSError:\n"
        "    network = 'blocked'\n"
        "status = pathlib.Path('/proc/self/status').read_text()\n"
        "fields = dict(line.split(':', 1) for line in status.splitlines() if ':' in line)\n"
        "pathlib.Path('sandbox.json').write_text(json.dumps({\n"
        "    'network': network,\n"
        "    'uid': os.getuid(),\n"
        "    'verification': os.environ.get('AETHER_VERIFICATION'),\n"
        "    'cap_eff': fields['CapEff'].strip(),\n"
        "    'no_new_privs': fields['NoNewPrivs'].strip(),\n"
        "}))\n",
        encoding="utf-8",
    )
    engine = VerificationEngine(repo=None, sandbox_mode="container")
    code, out = await engine._run(["python", "probe.py"], cwd=ws, timeout=180)

    if not (ws / "sandbox.json").exists():
        pytest.skip(f"container did not run (exit {code}): {out[:200]}")
    assert code == 0, out
    result = json.loads((ws / "sandbox.json").read_text(encoding="utf-8"))
    assert result == {
        "network": "blocked",
        "uid": 65534 if sys.platform == "win32" or os.getuid() == 0 else os.getuid(),
        "verification": "1",
        "cap_eff": "0000000000000000",
        "no_new_privs": "1",
    }


@pytest.mark.skipif(
    not _linux_container_runtime_available(),
    reason="no running Linux container engine on this machine",
)
@pytest.mark.skipif(
    not _container_image_available(CONTAINER_IMAGE),
    reason=f"{CONTAINER_IMAGE} is not built on this machine",
)
@pytest.mark.asyncio
async def test_container_timeout_removes_the_container(tmp_path: Path):
    runtime = shutil.which("docker") or shutil.which("podman")
    assert runtime is not None

    def running_verifiers() -> set[str]:
        result = subprocess.run(
            [runtime, "ps", "--filter", "name=aether-verify-", "--format", "{{.Names}}"],
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
        return {line for line in result.stdout.splitlines() if line}

    before = running_verifiers()
    engine = VerificationEngine(repo=None, sandbox_mode="container")
    code, out = await engine._run(
        ["python", "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        timeout=1,
    )

    assert code == 1
    assert "timed out" in out
    assert running_verifiers() == before


@pytest.mark.skipif(
    not (_linux_container_runtime_available() and _container_image_available(CONTAINER_IMAGE)),
    reason=f"the supported verifier image {CONTAINER_IMAGE!r} is not built",
)
@pytest.mark.asyncio
async def test_default_verifier_image_runs_pytest_without_network(tmp_path: Path):
    (tmp_path / "test_probe.py").write_text(
        "import os\n"
        "def test_verification_environment():\n"
        "    assert os.environ['AETHER_VERIFICATION'] == '1'\n",
        encoding="utf-8",
    )
    engine = VerificationEngine(repo=None, sandbox_mode="container")

    code, out = await engine._run(["pytest", "-q", "--tb=no"], cwd=tmp_path, timeout=180)

    assert code == 0, out
    assert "1 passed" in out


# --- WO-12: verification that fits the task ------------------------------


def _research_task(tmp_path: Path, goal: str, body: str):
    from aether.domain.models import Task

    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "findings.md").write_text(body, encoding="utf-8")
    return Task(original_prompt=goal, normalized_goal=goal, workspace_path=str(ws))


class _Recorder:
    """Stands in for the repository; verification only saves the result."""

    def __init__(self) -> None:
        self.saved = []

    async def save_result(self, result):
        self.saved.append(result)


@pytest.mark.asyncio
async def test_unsourced_research_fails_verification(tmp_path: Path):
    """The acceptance criterion for WO-12."""
    from aether.domain.enums import VerificationStatus

    task = _research_task(
        tmp_path,
        "Research the tradeoffs between Postgres and SQLite for desktop apps",
        "Postgres is better for desktop apps. SQLite is worse. Trust me.",
    )
    engine = VerificationEngine(repo=_Recorder())
    result = await engine.verify_task(task)

    assert result.status == VerificationStatus.FAILED
    sources = [c for c in result.checks if c.name == "research_sources"]
    assert sources and sources[0].status == VerificationStatus.FAILED
    assert "cannot be checked" in sources[0].detail


@pytest.mark.asyncio
async def test_sourced_research_passes(tmp_path: Path):
    from aether.domain.enums import VerificationStatus

    task = _research_task(
        tmp_path,
        "Research the tradeoffs between Postgres and SQLite for desktop apps",
        "# Findings\n\nSQLite suits single-writer desktop apps; Postgres suits\n"
        "concurrent writers.\n\nReferences\n- https://www.sqlite.org/whentouse.html\n"
        "- https://www.postgresql.org/docs/current/different-replication-solutions.html\n",
    )
    engine = VerificationEngine(repo=_Recorder())
    result = await engine.verify_task(task)

    names = {c.name: c.status for c in result.checks}
    assert names["research_sources"] == VerificationStatus.PASSED
    assert names["research_addresses_question"] == VerificationStatus.PASSED
    assert result.status == VerificationStatus.PASSED


@pytest.mark.asyncio
async def test_research_that_ignores_the_question_is_flagged(tmp_path: Path):
    """Sourced but off-topic is still not an answer."""
    from aether.domain.enums import VerificationStatus

    task = _research_task(
        tmp_path,
        "Research the tradeoffs between Postgres and SQLite for desktop apps",
        "# Unrelated\n\nA note on bicycle maintenance.\nReferences\n- https://example.com/bikes\n",
    )
    engine = VerificationEngine(repo=_Recorder())
    result = await engine.verify_task(task)

    coverage = [c for c in result.checks if c.name == "research_addresses_question"]
    assert coverage and coverage[0].status != VerificationStatus.PASSED


@pytest.mark.asyncio
async def test_code_tasks_do_not_get_research_checks(tmp_path: Path):
    task = _research_task(tmp_path, "Build a CLI that renames photos", "no sources here")
    engine = VerificationEngine(repo=_Recorder())
    result = await engine.verify_task(task)
    assert not [c for c in result.checks if c.name.startswith("research_")]
