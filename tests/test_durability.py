"""Durability: the work survives things that kill the process.

The project's central claim is that a task finishes even when the thing running
it dies. These tests are the proof. They start a real orchestrator in a child
process, SIGKILL it mid-subtask (no cleanup, no atexit, no flush), then bring a
fresh one up over the same data directory and require the task to finish.

A SIGKILL is deliberate: anything gentler lets Python run shutdown handlers,
which is exactly the case that already worked.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

# A worker that boots AETHER against a given data dir and drives one task.
# It prints the task id on stdout so the parent can watch progress, then blocks.
_RUNNER = textwrap.dedent(
    '''
    import asyncio, json, sys
    from pathlib import Path

    from aether.app import AetherApp
    from aether.config.settings import AetherSettings, OrchestratorSettings
    from aether.domain.enums import AdapterType, TaskMode, TaskStatus
    from aether.domain.models import ProviderConfig

    root = Path(sys.argv[1])
    delay = float(sys.argv[2])
    existing_task = sys.argv[3] if len(sys.argv) > 3 else ""

    def settings():
        s = AetherSettings(
            data_dir=root / "data",
            workspace_root=root / "workspace",
            database_url="sqlite+aiosqlite:///" + (root / "data" / "t.db").as_posix(),
            orchestrator=OrchestratorSettings(
                enable_git_checkpoints=False,
                enable_agent_branches=False,
                max_agent_retries=2,
            ),
        )
        s.providers = {
            "mock": ProviderConfig(
                provider_id="mock", display_name="Mock", adapter=AdapterType.MOCK,
                enabled=True, for_testing=True,
                capabilities={"coding": 9, "reasoning": 9, "architecture": 9,
                              "testing": 9, "review": 9, "research": 9},
            )
        }
        s.ensure_dirs()
        return s

    async def main():
        app = AetherApp(settings())
        await app.start()
        app.registry.get("mock").behavior["delay"] = delay

        if existing_task:
            # recovery run: app.start() already requeued non-terminal tasks
            task_id = existing_task
        else:
            task = await app.orchestrator.submit(
                "Build a small module", mode=TaskMode.SOLO
            )
            task_id = task.task_id
            print(json.dumps({"task_id": task_id}), flush=True)

        terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        for _ in range(1200):
            await asyncio.sleep(0.1)
            t = await app.tasks.get(task_id)
            if t and t.status in terminal:
                print(json.dumps({"final": t.status.value}), flush=True)
                break
        await app.stop()

    asyncio.run(main())
    '''
)


def _write_runner(tmp_path: Path) -> Path:
    script = tmp_path / "runner.py"
    script.write_text(_RUNNER, encoding="utf-8")
    return script


def _start(script: Path, root: Path, delay: float, task_id: str = "") -> subprocess.Popen:
    args = [sys.executable, str(script), str(root), str(delay)]
    if task_id:
        args.append(task_id)
    return subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )


def _read_task_id(proc: subprocess.Popen, timeout: float = 60.0) -> str:
    """First line of stdout carries the task id."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise AssertionError("runner exited before reporting a task id")
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if "task_id" in payload:
            return payload["task_id"]
    raise AssertionError("runner never reported a task id")


async def _load(root: Path, task_id: str):
    """Read final state directly from the database the runners shared."""
    from aether.app import AetherApp
    from aether.config.settings import AetherSettings, OrchestratorSettings
    from aether.domain.enums import AdapterType
    from aether.domain.models import ProviderConfig

    s = AetherSettings(
        data_dir=root / "data",
        workspace_root=root / "workspace",
        database_url="sqlite+aiosqlite:///" + (root / "data" / "t.db").as_posix(),
        orchestrator=OrchestratorSettings(enable_git_checkpoints=False),
    )
    s.providers = {
        "mock": ProviderConfig(
            provider_id="mock", display_name="Mock", adapter=AdapterType.MOCK,
            enabled=True, for_testing=True, capabilities={"coding": 9},
        )
    }
    s.ensure_dirs()
    app = AetherApp(s)
    # deliberately not app.start(): starting would recover and mutate the state
    # we are trying to observe
    await app.db.create_all()
    task = await app.tasks.get(task_id)
    events = await app.repo.list_events(task_id)
    await app.db.dispose()
    return task, events


@pytest.mark.asyncio
@pytest.mark.parametrize("kill_after", [0.6, 1.1, 1.7])
async def test_task_survives_sigkill_mid_subtask(tmp_path: Path, kill_after: float):
    """Hard-kill the orchestrator mid-subtask; a fresh one must finish the job."""
    root = tmp_path / "aether"
    root.mkdir()
    script = _write_runner(tmp_path)

    # slow agent so the kill lands inside an execution, not between them
    first = _start(script, root, delay=0.8)
    task_id = _read_task_id(first)
    time.sleep(kill_after)

    assert first.poll() is None, "runner finished before we could kill it"
    first.kill()
    first.wait(timeout=30)

    mid, _ = await _load(root, task_id)
    assert mid is not None, "task did not survive the kill at all"
    assert mid.status.value not in {"COMPLETED", "CANCELLED"}, (
        "kill landed after completion; test proves nothing"
    )

    # a fresh process over the same data dir, with a fast agent
    second = _start(script, root, delay=0.05, task_id=task_id)
    second.wait(timeout=180)

    final, events = await _load(root, task_id)
    assert final is not None
    assert final.status.value == "COMPLETED", (
        f"task did not recover: {final.status.value} / {final.failure_reason}"
    )

    # every subtask ran to completion exactly once
    completions = [e for e in events if e.action == "subtask_complete"]
    titles = [e.detail.split(" via ")[0] for e in completions]
    assert len(titles) == len(set(titles)), f"a subtask ran twice: {titles}"

    # nothing left stranded
    stranded = [s.title for s in final.subtasks if s.status.value == "RUNNING"]
    assert not stranded, f"subtasks stranded RUNNING after recovery: {stranded}"


@pytest.mark.asyncio
async def test_recovery_is_recorded_not_silent(tmp_path: Path):
    """An operator must be able to see that a recovery happened."""
    root = tmp_path / "aether"
    root.mkdir()
    script = _write_runner(tmp_path)

    first = _start(script, root, delay=0.8)
    task_id = _read_task_id(first)
    time.sleep(1.0)
    first.kill()
    first.wait(timeout=30)

    second = _start(script, root, delay=0.05, task_id=task_id)
    second.wait(timeout=180)

    final, events = await _load(root, task_id)
    assert final.status.value == "COMPLETED"
    actions = {e.action for e in events}
    assert "recovered" in actions or "reclaimed" in actions, (
        f"recovery left no trace on the timeline: {sorted(actions)}"
    )
