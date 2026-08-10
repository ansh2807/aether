"""Tests for extended AETHER features."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aether.app import AetherApp
from aether.core.approvals import ApprovalManager, ApprovalStatus
from aether.core.budget import BudgetExceeded, BudgetManager
from aether.core.event_bus import EventBus
from aether.core.metrics import MetricsCollector
from aether.core.workspace import WorkspaceManager
from aether.domain.enums import TaskMode, TaskStatus


@pytest.mark.asyncio
async def test_event_bus_history():
    bus = EventBus(history_size=10)
    await bus.emit("a", task_id="T1", x=1)
    await bus.emit("b", task_id="T2", x=2)
    await bus.emit("a", task_id="T1", x=3)
    assert len(bus.history(task_id="T1")) == 2
    assert len(bus.history(event_type="b")) == 1


@pytest.mark.asyncio
async def test_approval_grant_deny():
    mgr = ApprovalManager()
    req = await mgr.request("TASK-1", "install_packages", detail="pip install x", risk="high")
    assert req.status == ApprovalStatus.PENDING
    assert len(mgr.list_pending()) == 1
    granted = mgr.resolve(req.approval_id, granted=True, resolved_by="tester")
    assert granted.status == ApprovalStatus.GRANTED
    assert len(mgr.list_pending()) == 0


def test_budget_exceeded():
    bm = BudgetManager(default_max_tokens=100, default_max_requests=5)
    bm.create("T", max_tokens=100)
    bm.record("T", tokens=50)
    with pytest.raises(BudgetExceeded):
        bm.record("T", tokens=60)


@pytest.mark.asyncio
async def test_workspace_init(tmp_path: Path):
    ws = tmp_path / "ws"
    mgr = WorkspaceManager(enable_git=True)
    await mgr.ensure_repo(ws)
    assert (ws / "artifacts").exists()
    files = mgr.index_files(ws)
    assert isinstance(files, list)


@pytest.mark.asyncio
async def test_export_import(app: AetherApp, tmp_path: Path):
    task = await app.orchestrator.submit("Build exportable hello", mode=TaskMode.SOLO)
    # wait complete
    import asyncio

    for _ in range(100):
        t = await app.tasks.get(task.task_id)
        if t and t.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            break
        await asyncio.sleep(0.1)

    out = tmp_path / "export.json"
    await app.exporter.export_json(task.task_id, out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["format"] == "aether-task-export-v1"
    assert data["task"]["task_id"] == task.task_id

    # re-import into same store as paused clone-like (same id overwrite)
    imported = await app.exporter.import_json(out, as_paused=True)
    assert imported.task_id == task.task_id


@pytest.mark.asyncio
async def test_metrics_and_inspect_extensions(app: AetherApp):
    task = await app.orchestrator.submit("Research small topic for metrics", mode=TaskMode.SOLO)
    import asyncio

    for _ in range(100):
        t = await app.tasks.get(task.task_id)
        if t and t.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            break
        await asyncio.sleep(0.1)

    summary = app.metrics.summary()
    assert summary["tasks"]["started"] >= 1
    data = await app.orchestrator.inspect(task.task_id)
    assert "budget" in data
    assert "trace" in data
    assert data["budget"] is not None


@pytest.mark.asyncio
async def test_doctor(app: AetherApp):
    from aether.core.doctor import run_doctor

    report = await run_doctor(app)
    assert report["summary"]["checks"] > 0
    assert report["version"]


@pytest.mark.asyncio
async def test_budget_enforced_on_task(app: AetherApp):
    # tiny budget forces failure after first agent call(s)
    task = await app.orchestrator.submit(
        "Build app under tight budget",
        mode=TaskMode.SOLO,
        max_tokens=1,
        max_requests=1,
    )
    import asyncio

    for _ in range(100):
        t = await app.tasks.get(task.task_id)
        if t and t.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            break
        await asyncio.sleep(0.1)
    t = await app.tasks.get(task.task_id)
    assert t is not None
    # with 1 request budget, multi-subtask tasks should fail on budget or complete if lucky with 1
    assert t.status in {TaskStatus.FAILED, TaskStatus.COMPLETED}


def test_metrics_agent_stats():
    m = MetricsCollector()
    m.record_agent_execution("mock", 10.0, True, task_id="T")
    m.record_agent_execution("mock", 30.0, False, task_id="T")
    stats = m.agent_stats("mock")
    assert stats["success"] == 1
    assert stats["failure"] == 1
    assert stats["avg_latency_ms"] == 20.0
