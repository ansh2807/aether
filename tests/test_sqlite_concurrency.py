"""SQLite must not fail a read just because something else is writing.

Two CI runs failed intermittently with `sqlite3.OperationalError: database is
locked`, in two unrelated tests, both times on a *read* that raced an
orchestrator write:

    tests/test_planner.py::test_orchestrator_call_site_falls_back_when_no_real_provider
    tests/test_failure_scenarios.py::test_18_cancel_mid_flight

The cause was SQLite's defaults, measured as journal_mode=delete and
busy_timeout=5000. Under a rollback journal a writer takes an exclusive lock
and blocks readers outright; on a loaded CI runner a transaction can easily
outlive a five-second patience budget. Neither test was wrong.

These tests pin the fix rather than the symptom, and it is worth being precise
about which of them can actually catch a regression.

Restoring the old settings (journal_mode=DELETE, busy_timeout=5000) was tried,
and **only `test_sqlite_uses_wal_and_a_generous_busy_timeout` failed.** The
concurrency tests below passed against the broken configuration too, so they
are smoke tests, not proof.

The reason is worth recording, because it is easy to write a test that looks
like it reproduces this and does not: under a rollback journal a writer holds
only a RESERVED lock for the body of its transaction, and escalates to
EXCLUSIVE just for the commit. Readers are locked out during that narrow
window, not for the duration of the write. Reproducing it on demand means
landing inside a window of a few milliseconds, which is exactly why it showed
up as an intermittent CI failure and not as a reliable bug.

So the pragma assertion is the real guard here: it fails the moment the
configuration regresses, which is the thing that caused the outage.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from aether.storage.db import Database
from aether.storage.models import TaskRow


def _url(tmp_path: Path) -> str:
    return "sqlite+aiosqlite:///" + (tmp_path / "t.db").as_posix()


def _row(task_id: str) -> TaskRow:
    now = datetime.now(timezone.utc)
    return TaskRow(
        task_id=task_id, payload_json="{}", status="QUEUED",
        priority=1, progress=0.0, created_at=now, updated_at=now,
    )


@pytest.mark.asyncio
async def test_sqlite_uses_wal_and_a_generous_busy_timeout(tmp_path: Path):
    db = Database(_url(tmp_path))
    await db.create_all()
    try:
        async with db.session() as s:
            journal = (await s.execute(text("PRAGMA journal_mode"))).scalar()
            busy = (await s.execute(text("PRAGMA busy_timeout"))).scalar()
            sync = (await s.execute(text("PRAGMA synchronous"))).scalar()

        assert str(journal).lower() == "wal", (
            f"journal_mode={journal}; a rollback journal blocks readers behind writers"
        )
        assert busy >= 30_000, f"busy_timeout={busy}ms is not enough under CI load"
        # 2 == FULL. Durability is the project's central promise, so WAL must
        # not have quietly dragged synchronous down to NORMAL with it.
        assert sync == 2, f"synchronous={sync}; expected FULL (2)"
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_a_read_survives_a_long_running_write(tmp_path: Path):
    """A reader must not be starved by a write that outlives the busy timeout.

    Verified as a smoke test, not as a reproduction: this also passes under
    journal_mode=DELETE, because the writer holds RESERVED (not EXCLUSIVE) for
    the body of its transaction and readers are only blocked during the brief
    commit. See the module docstring. Kept because it would catch a genuinely
    reader-hostile configuration, and it costs 7 seconds once.
    """
    db = Database(_url(tmp_path))
    await db.create_all()
    async with db.session() as s:
        s.add(_row("EXISTING"))

    write_started = asyncio.Event()

    async def slow_writer() -> None:
        async with db.session() as s:
            s.add(_row("SLOW-WRITER"))
            await s.flush()          # take the write lock
            write_started.set()
            await asyncio.sleep(7)   # hold it past the old 5s budget

    async def reader() -> str:
        await write_started.wait()
        await asyncio.sleep(0.5)
        async with db.session() as s:
            got = (await s.execute(text("SELECT task_id FROM tasks WHERE task_id='EXISTING'"))).scalar()
        return got

    try:
        _, seen = await asyncio.gather(slow_writer(), reader())
        assert seen == "EXISTING", "the reader did not see committed data"
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_many_concurrent_writers_all_land(tmp_path: Path):
    """Writers still serialise under WAL; they must queue, not fail."""
    db = Database(_url(tmp_path))
    await db.create_all()

    async def writer(n: int) -> None:
        for i in range(20):
            async with db.session() as s:
                s.add(_row(f"T{n}-{i}"))

    try:
        results = await asyncio.gather(*(writer(n) for n in range(12)),
                                       return_exceptions=True)
        failures = [r for r in results if isinstance(r, Exception)]
        assert not failures, f"{len(failures)} writers failed, first: {failures[0]!r}"

        async with db.session() as s:
            count = (await s.execute(text("SELECT COUNT(*) FROM tasks"))).scalar()
        assert count == 12 * 20, f"expected 240 rows, found {count} — writes were lost"
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_postgres_urls_never_see_sqlite_pragmas():
    """The portability guarantee: the fix must be invisible to other dialects.

    Builds the engine without connecting, which is enough — the guard is on the
    dialect name, so no pragma listener is ever registered for Postgres.
    """
    pytest.importorskip("asyncpg", reason="postgres extra not installed")

    from unittest.mock import patch

    db = Database("postgresql+asyncpg://user:pw@localhost:5432/nope")
    assert db.engine.dialect.name == "postgresql"

    # Re-run the configurator and prove it registers nothing.
    with patch("sqlalchemy.event.listens_for") as listens_for:
        db._configure_sqlite()
    listens_for.assert_not_called()

    await db.dispose()


@pytest.mark.asyncio
async def test_sqlite_does_register_the_listener(tmp_path: Path):
    """The mirror of the test above — otherwise it would pass on a no-op."""
    from unittest.mock import patch

    db = Database(_url(tmp_path))
    with patch("sqlalchemy.event.listens_for") as listens_for:
        db._configure_sqlite()
    listens_for.assert_called_once()
    await db.dispose()
