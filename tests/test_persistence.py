"""Control-plane state must survive the process that produced it.

The CLI and the API server are separate processes over one data dir, so
approvals, metrics, budgets and provider usage all have to round-trip.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from aether.config.settings import AetherSettings, QuotaSettings
from aether.core.approvals import ApprovalManager, ApprovalStatus
from aether.core.budget import BudgetManager
from aether.core.metrics import MetricsCollector
from aether.core.quota import QuotaManager
from aether.core.state_store import JsonStateStore
from aether.domain.enums import ProviderAvailability
from aether.domain.models import utcnow


def _store(tmp_path: Path, name: str) -> JsonStateStore:
    return JsonStateStore(tmp_path / "state" / name)


# --- JSON store ----------------------------------------------------------


def test_store_survives_corrupt_file(tmp_path: Path):
    store = _store(tmp_path, "x.json")
    store.save({"a": 1})
    store.path.write_text("{not json", encoding="utf-8")
    assert store.load() == {}
    store.save({"a": 2})
    assert store.load() == {"a": 2}


# --- #6 approvals across processes ---------------------------------------


@pytest.mark.asyncio
async def test_approval_visible_to_a_second_process(tmp_path: Path):
    store_path = tmp_path / "state" / "approvals.json"
    server = ApprovalManager(store=JsonStateStore(store_path))
    req = await server.request("TASK-1", operation="start_task", detail="run it")

    # a separate process (fresh manager over the same file)
    cli = ApprovalManager(store=JsonStateStore(store_path))
    pending = cli.list_pending()
    assert [a.approval_id for a in pending] == [req.approval_id]

    cli.resolve(req.approval_id, granted=True, resolved_by="cli-user")

    # the server side sees the resolution
    assert server.list_pending() == []
    resolved = server.get(req.approval_id)
    assert resolved is not None
    assert resolved.status == ApprovalStatus.GRANTED
    assert resolved.resolved_by == "cli-user"


@pytest.mark.asyncio
async def test_approval_without_store_stays_in_memory(tmp_path: Path):
    mgr = ApprovalManager()
    req = await mgr.request("TASK-1", operation="start_task")
    assert [a.approval_id for a in mgr.list_pending()] == [req.approval_id]
    assert not (tmp_path / "state").exists()


# --- #6 metrics and budgets ----------------------------------------------


def test_metrics_round_trip_between_processes(tmp_path: Path):
    path = tmp_path / "state" / "metrics.json"
    server = MetricsCollector(store=JsonStateStore(path))
    server.record_task_event("TASK-1", "started")
    server.record_agent_execution("openai", 120.0, True, task_id="TASK-1")
    server.record_task_event("TASK-1", "completed")

    cli = MetricsCollector(store=JsonStateStore(path))
    summary = cli.summary()
    assert summary["tasks"]["started"] == 1
    assert summary["tasks"]["completed"] == 1
    assert [a["provider_id"] for a in summary["agents"]] == ["openai"]
    trace = cli.task_trace("TASK-1")
    assert [row["event"] for row in trace] == ["started", "agent_execution", "completed"]


def test_budget_round_trip_between_processes(tmp_path: Path):
    path = tmp_path / "state" / "budgets.json"
    server = BudgetManager(store=JsonStateStore(path))
    server.create("TASK-1", max_tokens=1000, max_requests=10)
    server.record("TASK-1", tokens=250, requests=1)

    cli = BudgetManager(store=JsonStateStore(path))
    snap = cli.snapshot("TASK-1")
    assert snap is not None
    assert snap["used_tokens"] == 250
    assert snap["max_tokens"] == 1000


# --- #5 quota daily reset + persistence -----------------------------------


def _quota_settings(tmp_path: Path) -> AetherSettings:
    return AetherSettings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspace",
        quota=QuotaSettings(default_requests_per_minute=1000, default_daily_requests=3),
    )


def test_daily_quota_resets_at_utc_midnight(tmp_path: Path):
    q = QuotaManager(_quota_settings(tmp_path))
    for _ in range(3):
        q.record_request("openai")
    assert q.availability("openai") == ProviderAvailability.QUOTA_EXHAUSTED

    # roll the clock: the stored counter belongs to yesterday
    usage = q.get_usage("openai")
    usage.day = (utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

    assert q.availability("openai") == ProviderAvailability.AVAILABLE
    assert q.get_usage("openai").daily_requests == 0


def test_quota_usage_survives_restart(tmp_path: Path):
    settings = _quota_settings(tmp_path)
    path = tmp_path / "state" / "usage.json"
    first = QuotaManager(settings, store=JsonStateStore(path))
    first.record_request("openai", tokens=100)
    first.record_request("openai", tokens=100)

    restarted = QuotaManager(settings, store=JsonStateStore(path))
    assert restarted.get_usage("openai").daily_requests == 2
    assert restarted.get_usage("openai").estimated_tokens == 200
    # one more request tips it over the daily limit of 3
    restarted.record_request("openai")
    assert restarted.availability("openai") == ProviderAvailability.QUOTA_EXHAUSTED


def test_quota_cooldown_survives_restart(tmp_path: Path):
    settings = _quota_settings(tmp_path)
    path = tmp_path / "state" / "usage.json"
    first = QuotaManager(settings, store=JsonStateStore(path))
    first.set_cooldown("grok", 300)

    restarted = QuotaManager(settings, store=JsonStateStore(path))
    assert restarted.availability("grok") == ProviderAvailability.RATE_LIMITED


# --- cross-process locking -------------------------------------------------


def test_transaction_is_reentrant(tmp_path: Path):
    """A locked write path may call another locked helper without deadlocking."""
    store = _store(tmp_path, "reentrant.json")
    with store.transaction():
        with store.transaction():
            store.save({"n": 1})
    assert store.load() == {"n": 1}


def test_update_applies_atomically(tmp_path: Path):
    store = _store(tmp_path, "counter.json")

    def bump(data):
        data["n"] = data.get("n", 0) + 1

    for _ in range(5):
        store.update(bump)
    assert store.load()["n"] == 5


_WORKER = """
import sys
from aether.core.state_store import JsonStateStore

store = JsonStateStore(sys.argv[1])
for _ in range(int(sys.argv[2])):
    store.update(lambda d: d.__setitem__("n", d.get("n", 0) + 1))
"""


def test_concurrent_processes_do_not_lose_updates(tmp_path: Path):
    """Two processes incrementing the same counter must both be counted."""
    import subprocess
    import sys

    script = tmp_path / "worker.py"
    script.write_text(_WORKER, encoding="utf-8")
    target = tmp_path / "state" / "shared.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    per_worker, workers = 40, 3
    procs = [
        subprocess.Popen([sys.executable, str(script), str(target), str(per_worker)])
        for _ in range(workers)
    ]
    for proc in procs:
        assert proc.wait(timeout=120) == 0

    assert JsonStateStore(target).load()["n"] == per_worker * workers
