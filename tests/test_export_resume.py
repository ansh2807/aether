"""WO-05 — cross-machine resume, proven.

`TaskExporter` claims a task can be moved to another machine mid-flight and
finish there. This module is the round trip that actually proves it: two
fully separate `AetherApp`s, over two fully separate `data_dir`s and
`workspace_root`s, connected by nothing but the exported bundle file.
"""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

import pytest

from aether.app import AetherApp
from aether.config.settings import (
    AetherSettings,
    CircuitBreakerSettings,
    OrchestratorSettings,
    QuotaSettings,
    SecuritySettings,
)
from aether.domain.enums import AdapterType, SubtaskStatus, TaskMode, TaskStatus
from aether.domain.models import ProviderConfig


def _make_settings(base_dir: Path) -> AetherSettings:
    """Build a fully self-contained settings object rooted at base_dir.

    Mirrors tests/conftest.py's `settings` fixture, but as a plain function so
    two independent "machines" (two independent base_dir trees) can be built
    inside a single test without sharing anything but the file handed between
    them.
    """
    data = base_dir / "data"
    ws = base_dir / "workspace"
    data.mkdir(parents=True, exist_ok=True)
    ws.mkdir(parents=True, exist_ok=True)
    db = data / "test.db"
    s = AetherSettings(
        data_dir=data,
        workspace_root=ws,
        database_url=f"sqlite+aiosqlite:///{db.as_posix()}",
        orchestrator=OrchestratorSettings(
            max_agent_retries=2,
            max_verification_loops=2,
            agent_timeout_seconds=30,
            enable_git_checkpoints=False,
        ),
        circuit_breaker=CircuitBreakerSettings(failure_threshold=3, recovery_timeout_seconds=1),
        quota=QuotaSettings(default_requests_per_minute=1000, default_daily_requests=10000),
        security=SecuritySettings(approval_level="safe"),
    )
    s.providers = {
        "mock": ProviderConfig(
            provider_id="mock",
            display_name="Mock",
            adapter=AdapterType.MOCK,
            enabled=True,
            for_testing=True,
            capabilities={
                "coding": 9,
                "reasoning": 9,
                "research": 8,
                "testing": 9,
                "terminal": 8,
                "review": 8,
                "architecture": 8,
            },
            roles_preferred=[
                "architect",
                "implementer",
                "tester",
                "reviewer",
                "debugger",
                "security_reviewer",
                "final_reviewer",
                "researcher",
                "general",
            ],
        ),
    }
    s.ensure_dirs()
    return s


async def _wait_for(condition, timeout_s: float = 10.0, interval_s: float = 0.02):
    elapsed = 0.0
    while elapsed < timeout_s:
        result = await condition()
        if result:
            return result
        await asyncio.sleep(interval_s)
        elapsed += interval_s
    raise AssertionError(f"condition not met within {timeout_s}s")


@pytest.mark.asyncio
async def test_cross_machine_resume_round_trip(tmp_path_factory: pytest.TempPathFactory, tmp_path: Path):
    # --- Machine A: fully independent data_dir + workspace_root ---
    machine_a_root = tmp_path_factory.mktemp("machine_a")
    settings_a = _make_settings(machine_a_root)
    app_a = AetherApp(settings_a)
    await app_a.start()

    try:
        task = await app_a.orchestrator.submit(
            "Build a small exportable widget", mode=TaskMode.SOLO
        )
        task_id = task.task_id

        # Pause once the *first* subtask has genuinely completed, so what
        # gets exported is a half-finished task: one subtask COMPLETED, the
        # rest still pending.
        #
        # This sets the orchestrator's own stop flag directly rather than
        # calling `orchestrator.pause_task()` from here. `pause_task` (via
        # `TaskManager.pause`/`transition`) reads the task with a plain
        # `get_task` and writes it back with a plain `save_task` — unlike
        # every subtask mutation, which goes through
        # `SqliteRepository.update_task`'s per-task lock. Firing it from
        # outside the runner while a subtask's own locked read-modify-write
        # is still in flight is a genuine last-writer-wins race: it was
        # caught here reverting a just-completed subtask back to RUNNING
        # (see the report for this work order). Setting the flag and letting
        # the orchestrator's own loop perform the pause — which it only ever
        # does at a clean boundary between subtasks, never concurrently with
        # one — sidesteps that race entirely.
        stop_flag = app_a.orchestrator._stop_flags.get(task_id)
        assert stop_flag is not None, "orchestrator should have registered a stop flag for the running task"

        async def _first_subtask_completed():
            t = await app_a.tasks.get(task_id)
            if t and any(s.status == SubtaskStatus.COMPLETED for s in t.subtasks):
                return t
            return None

        await _wait_for(_first_subtask_completed, timeout_s=15.0)
        stop_flag.set()

        async def _is_paused():
            t = await app_a.tasks.get(task_id)
            return t if t and t.status == TaskStatus.PAUSED else None

        task_a = await _wait_for(_is_paused, timeout_s=15.0)

        completed = [s for s in task_a.subtasks if s.status == SubtaskStatus.COMPLETED]
        unfinished = [
            s
            for s in task_a.subtasks
            if s.status not in (SubtaskStatus.COMPLETED, SubtaskStatus.SKIPPED, SubtaskStatus.CANCELLED)
        ]
        assert completed, "expected at least one completed subtask before the move"
        assert unfinished, "expected at least one unfinished subtask — this must be a half-finished task"
        assert 0.0 < task_a.progress < 1.0

        ws_a = Path(task_a.workspace_path)
        assert ws_a.exists()

        # A distinctive artifact that nothing running later will touch or
        # regenerate, so its survival specifically proves the *pre-move*
        # workspace made the trip — not that some later run happened to
        # recreate a file with the same name.
        marker = ws_a / "artifacts" / "PRE_MOVE_MARKER.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker_content = f"created on machine A for {task_id} before the move"
        marker.write_text(marker_content, encoding="utf-8")

        # The mock agent's own artifact from the completed subtask.
        notes_path = ws_a / "AGENT_NOTES.md"
        assert notes_path.exists(), "mock adapter should have written an artifact for the completed subtask"
        notes_before = notes_path.read_text(encoding="utf-8")

        events_a = await app_a.repo.list_events(task_id)
        handoffs_a = await app_a.repo.list_handoffs(task_id)
        checkpoints_a = await app_a.repo.list_checkpoints(task_id)
        assert handoffs_a, "expected at least one handoff from the completed subtask"
        assert checkpoints_a, "expected at least one checkpoint from the completed subtask"

        bundle_path = tmp_path / "handoff-bundle.zip"
        await app_a.exporter.export_bundle(task_id, bundle_path, workspace=ws_a)
        assert bundle_path.exists()

        # Sanity: the bundle really does carry the workspace, including the
        # pre-move marker, inside the zip itself (not just on disk still).
        with zipfile.ZipFile(bundle_path, "r") as zf:
            names = zf.namelist()
            assert "task.json" in names
            assert "workspace/artifacts/PRE_MOVE_MARKER.txt" in names
            assert "workspace/AGENT_NOTES.md" in names
    finally:
        await app_a.stop()

    # --- Machine B: a *different* machine. No shared data_dir, no shared
    # workspace_root, no shared process state — only the bundle file crosses
    # over. ---
    machine_b_root = tmp_path_factory.mktemp("machine_b")
    settings_b = _make_settings(machine_b_root)
    assert settings_b.data_dir != settings_a.data_dir
    assert settings_b.workspace_root != settings_a.workspace_root
    app_b = AetherApp(settings_b)
    await app_b.start()

    try:
        imported = await app_b.exporter.import_bundle(bundle_path, settings_b.workspace_root)
        assert imported.task_id == task_id

        # The whole point: the source machine's absolute workspace path must
        # not survive the move, and the new one must actually exist here.
        assert imported.workspace_path != str(ws_a)
        assert imported.workspace_path != task_a.workspace_path
        ws_b = Path(imported.workspace_path)
        assert str(ws_b).startswith(str(settings_b.workspace_root.resolve()))
        assert ws_b.exists()

        # Pre-move artifacts present on the destination *before* anything on
        # B has run — this proves the import itself carried them over, not a
        # later re-run that happened to reproduce them.
        imported_marker = ws_b / "artifacts" / "PRE_MOVE_MARKER.txt"
        assert imported_marker.exists()
        assert imported_marker.read_text(encoding="utf-8") == marker_content

        imported_notes = ws_b / "AGENT_NOTES.md"
        assert imported_notes.exists()
        assert imported_notes.read_text(encoding="utf-8") == notes_before

        # Handoff trail, checkpoints and timeline all survived the move.
        events_b = await app_b.repo.list_events(task_id)
        handoffs_b = await app_b.repo.list_handoffs(task_id)
        checkpoints_b = await app_b.repo.list_checkpoints(task_id)

        event_ids_a = {e.event_id for e in events_a}
        event_ids_b = {e.event_id for e in events_b}
        assert event_ids_a <= event_ids_b, "timeline from A must be intact on B"

        handoff_ids_a = {h.handoff_id for h in handoffs_a}
        handoff_ids_b = {h.handoff_id for h in handoffs_b}
        assert handoff_ids_a <= handoff_ids_b, "handoff trail from A must be intact on B"

        checkpoint_ids_a = {c.checkpoint_id for c in checkpoints_a}
        checkpoint_ids_b = {c.checkpoint_id for c in checkpoints_b}
        assert checkpoint_ids_a <= checkpoint_ids_b, "checkpoints from A must be intact on B"

        # Resume on the destination machine and let it run to completion.
        await app_b.orchestrator.resume_task(task_id)

        async def _is_terminal():
            t = await app_b.tasks.get(task_id)
            return t if t and t.status in {TaskStatus.COMPLETED, TaskStatus.FAILED} else None

        final_task = await _wait_for(_is_terminal, timeout_s=20.0)

        assert final_task.status == TaskStatus.COMPLETED, (
            f"resumed task did not complete on the destination machine: "
            f"{final_task.failure_reason}"
        )
        assert all(
            s.status in (SubtaskStatus.COMPLETED, SubtaskStatus.SKIPPED, SubtaskStatus.CANCELLED)
            for s in final_task.subtasks
        )

        # The pre-move artifact is still there after the rest of the task
        # ran to completion on the destination machine.
        assert imported_marker.exists()
        assert imported_marker.read_text(encoding="utf-8") == marker_content

        # New work landed in the same, correctly-rewritten workspace — not
        # back at the stale source-machine path.
        assert (ws_b / "artifacts" / "FINAL_RESULT.md").exists()
    finally:
        await app_b.stop()
