"""Demo: kill the process mid-flight, and the task still finishes.

    python scripts/demo_failover.py

That is the one claim AETHER is built around, so it should be reproducible by a
stranger in one command rather than taken on trust. No API keys: two mock
providers stand in for two real ones. What is being demonstrated is the
orchestrator's recovery path, and that path does not know or care whether the
adapter behind it was a mock, a CLI or an HTTP API.

What happens:

  1. A task starts in a child process and gets partway through its subtasks.
  2. The child is killed outright — SIGKILL on Linux/macOS, TerminateProcess on
     Windows. No cleanup, no atexit, no flush, no `finally`.
  3. A fresh process opens the same database. Startup requeues whatever was
     left non-terminal.
  4. The task resumes from its last checkpoint and reaches COMPLETED.

Note what is *not* claimed. Recovery may well pick the same provider it died
on — routing scores capability, health and quota, and a killed process is not
evidence against the provider. Two providers are configured so that it *can*
move, not to promise that it will. Provider failover is a different mechanism,
covered in tests/test_durability.py and docs/DURABILITY.md.

This mirrors tests/test_durability.py, which is the version CI runs. The script
exists so the claim can be watched rather than only asserted.

To record it:

    asciinema rec aether-failover.cast -c "python scripts/demo_failover.py"
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

RULE = "=" * 70

# Child process: boots AETHER over a given data dir, drives one task, blocks.
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
    resuming = len(sys.argv) > 3

    def _provider(pid, name):
        return ProviderConfig(
            provider_id=pid, display_name=name, adapter=AdapterType.MOCK,
            enabled=True, for_testing=True,
            capabilities={"coding": 9, "reasoning": 9, "architecture": 9,
                          "testing": 9, "review": 9, "research": 9},
        )

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
        # two providers, so recovery can land somewhere other than where it died
        s.providers = {"mock_a": _provider("mock_a", "Provider A"),
                       "mock_b": _provider("mock_b", "Provider B")}
        s.ensure_dirs()
        return s

    async def main():
        app = AetherApp(settings())
        await app.start()          # also requeues anything left non-terminal
        for pid in ("mock_a", "mock_b"):
            app.registry.get(pid).behavior["delay"] = delay

        if not resuming:
            task = await app.orchestrator.submit("Build a small module", mode=TaskMode.SOLO)
            print(json.dumps({"task_id": task.task_id}), flush=True)
            task_id = task.task_id
        else:
            task_id = sys.argv[3]

        terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        for _ in range(1800):
            await asyncio.sleep(0.1)
            t = await app.tasks.get(task_id)
            if t and t.status in terminal:
                print(json.dumps({"final": t.status.value}), flush=True)
                break
        await app.stop()

    asyncio.run(main())
    '''
)


async def _inspect(root: Path, task_id: str):
    """Read state straight from the shared database, without starting anything.

    Deliberately avoids app.start(): starting would trigger recovery and mutate
    the very state we are trying to photograph.
    """
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
        "mock_a": ProviderConfig(
            provider_id="mock_a", display_name="A", adapter=AdapterType.MOCK,
            enabled=True, for_testing=True, capabilities={"coding": 9},
        )
    }
    s.ensure_dirs()
    app = AetherApp(s)
    await app.db.create_all()
    task = await app.tasks.get(task_id)
    events = await app.repo.list_events(task_id)
    handoffs = await app.repo.list_handoffs(task_id)
    await app.db.dispose()
    return task, events, handoffs


def _summarise(task) -> str:
    done = sum(1 for s in task.subtasks if s.status.value == "COMPLETED")
    agents = sorted({s.assigned_agent for s in task.subtasks if s.assigned_agent})
    return (f"status={task.status.value}  subtasks={done}/{len(task.subtasks)}  "
            f"agents={', '.join(agents) or '-'}")


def main() -> int:
    import asyncio

    root = Path(tempfile.mkdtemp(prefix="aether-demo-"))
    runner = root / "runner.py"
    runner.write_text(_RUNNER, encoding="utf-8")

    print(f"\n{RULE}\n AETHER — a task outliving the process that started it\n{RULE}")
    print(f"\n scratch : {root}")
    print(" keys    : none needed; two mock providers stand in for two real ones")

    # --- 1. start ---------------------------------------------------------
    print("\n[1/4] starting a task in a child process...")
    # Each subtask takes ~6s, so there is a real window to kill inside. An
    # earlier version used 1.5s and the task finished before the kill landed —
    # the demo then "passed" having demonstrated nothing at all.
    proc = subprocess.Popen(
        [sys.executable, str(runner), str(root), "6.0"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    task_id = ""
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and not task_id:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                print("      child exited before reporting a task id")
                return 1
            continue
        try:
            task_id = json.loads(line).get("task_id", "")
        except ValueError:
            continue
    if not task_id:
        proc.kill()
        print("      child never reported a task id")
        return 1
    print(f"      task {task_id} is running")

    # Wait for a genuinely mid-flight moment: some progress banked, work still
    # outstanding. Killing a finished task would prove nothing, so refuse to.
    print("      waiting for it to get partway (some done, some not)...")
    midflight = False
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        time.sleep(1.0)
        snapshot, _, _ = asyncio.run(_inspect(root, task_id))
        if snapshot is None:
            continue
        done = sum(1 for s in snapshot.subtasks if s.status.value == "COMPLETED")
        total = len(snapshot.subtasks)
        if snapshot.status.value in {"COMPLETED", "FAILED", "CANCELLED"}:
            break
        if total and 0 < done < total:
            print(f"      caught it at {done}/{total} subtasks — killing now")
            midflight = True
            break

    if not midflight:
        proc.kill()
        print("\n      Could not catch the task mid-flight; it finished too fast.")
        print("      Nothing would be demonstrated by killing it now, so stopping")
        print("      rather than printing a PASS that means nothing.")
        print("      Raise the delay argument in _RUNNER and try again.")
        return 1

    # --- 2. kill ----------------------------------------------------------
    verb = "TerminateProcess" if sys.platform == "win32" else "SIGKILL"
    print(f"\n[2/4] {verb} to pid {proc.pid} — no cleanup, no flush, no finally.")
    proc.kill()
    proc.wait(timeout=30)
    print("      the process is gone.")

    # --- 3. photograph the wreckage --------------------------------------
    print("\n[3/4] reading the database left behind:")
    task, events, _ = asyncio.run(_inspect(root, task_id))
    print(f"      {_summarise(task)}")
    print(f"      timeline events written before the kill: {len(events)}")
    incomplete = [s for s in task.subtasks if s.status.value != "COMPLETED"]
    print(f"      unfinished subtasks stranded by the kill: {len(incomplete)}")

    # --- 4. recover -------------------------------------------------------
    print("\n[4/4] fresh process over the same data dir; startup requeues it...")
    resumed = subprocess.Popen(
        [sys.executable, str(runner), str(root), "0.3", task_id],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    try:
        resumed.wait(timeout=300)
    except subprocess.TimeoutExpired:
        resumed.kill()

    final, events_after, handoffs = asyncio.run(_inspect(root, task_id))
    partial = [h for h in handoffs if getattr(h.status, "value", h.status) == "PARTIAL"]

    print(f"\n{RULE}\n Result\n{RULE}")
    print(f"  before kill : {_summarise(task)}")
    print(f"  after resume: {_summarise(final)}")
    print(f"  timeline    : {len(events)} events -> {len(events_after)} events")
    if partial:
        print(f"  salvaged    : {len(partial)} PARTIAL handoff(s) — output from the "
              f"killed subtask was folded in rather than redone")
    else:
        print("  salvaged    : 0 PARTIAL handoffs — the interrupted subtask was "
              "re-run from its\n                last checkpoint rather than "
              "part-salvaged. Salvage needs\n                in-flight output on "
              "disk, which the mock adapter does not\n                produce; the CLI "
              "adapter does. Recovery does not depend on it.")

    ok = final.status.value == "COMPLETED"
    print("\n  " + ("PASS — the task outlived the process that started it."
                    if ok else
                    f"FAIL — ended {final.status.value}. That is a real bug; please file it."))
    print(f"\n  database left at {root} if you want to poke at it.\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
