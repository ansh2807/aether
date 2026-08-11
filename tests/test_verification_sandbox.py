"""WO-13: the verification gate must not hand its secrets to the code it verifies.

Verification exists because the agent's word is not evidence. Running that same
untrusted code with the operator's full environment — every provider API key on
the machine — undoes the point of the exercise.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from aether.core.verification import VerificationEngine

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
async def test_container_mode_degrades_loudly_without_a_runtime(tmp_path: Path, monkeypatch):
    """Silently downgrading someone's isolation is the worst possible outcome."""
    monkeypatch.setattr(VerificationEngine, "_container_runtime", lambda self: None)
    ws = _workspace(tmp_path)

    engine = VerificationEngine(repo=None, sandbox_mode="container")
    code, out = await engine._run([sys.executable, "peek.py"], cwd=ws, timeout=60)

    assert code == 0
    assert "container runtime not found" in out, "the downgrade was not reported"
    seen = (ws / "seen.json").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY" not in seen, "downgrade must still scrub"


@pytest.mark.skipif(
    not (shutil.which("docker") or shutil.which("podman")),
    reason="no container runtime on this machine",
)
@pytest.mark.asyncio
async def test_container_mode_has_no_network(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "net.py").write_text(
        "import socket, pathlib\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "    pathlib.Path('net.txt').write_text('reachable')\n"
        "except OSError:\n"
        "    pathlib.Path('net.txt').write_text('blocked')\n",
        encoding="utf-8",
    )
    engine = VerificationEngine(repo=None, sandbox_mode="container")
    await engine._run(["python", "net.py"], cwd=ws, timeout=180)
    assert (ws / "net.txt").read_text(encoding="utf-8") == "blocked"


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
