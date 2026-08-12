"""SQLAlchemy async engine and session.

Backend-agnostic by design: every query here is plain SQLAlchemy Core/ORM with
no dialect branches. `Database` is handed a `settings.database_url` and builds
an async engine for whatever dialect that URL names — `sqlite+aiosqlite://`
(the zero-setup default) or `postgresql+asyncpg://` (opt into it with the
`postgres` extra: `pip install aether[postgres]`). See `storage/repository.py`
and `storage/models.py` for the rest of the portability story — the ORM layer
has no dialect-specific branches either.

The one deliberate exception is `_configure_sqlite`, which sets WAL and a busy
timeout on SQLite connections only. That is not a portability leak but a fix
for a real intermittent failure: SQLite's default rollback journal has writers
block readers, which produced `database is locked` in CI. It hangs off a
connect event guarded by the dialect name, so Postgres never sees it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def _normalize_database_url(database_url: str) -> str:
    """Accept the common Postgres URL spellings, not just SQLAlchemy's.

    `postgres://` (Heroku/most hosted-Postgres providers) and a bare
    `postgresql://` (psycopg's sync scheme, the one everyone types first)
    both need the `+asyncpg` driver suffix to work with an async engine.
    Rewriting them here means `AETHER_DATABASE_URL=postgres://...` — the URL
    a user copies straight out of their Postgres host's dashboard — just
    works, instead of failing with an opaque "no async dialect" error.
    SQLite URLs pass through untouched.
    """
    if database_url.startswith("postgres://"):
        return "postgresql+asyncpg://" + database_url[len("postgres://") :]
    if database_url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + database_url[len("postgresql://") :]
    return database_url


class Database:
    def __init__(self, database_url: str) -> None:
        self.database_url = _normalize_database_url(database_url)
        # pool_pre_ping: cheap SELECT 1 before handing out a pooled
        # connection. A no-op cost-wise for SQLite's single file connection;
        # it matters for Postgres, where a connection can go stale (service
        # container restart, idle timeout, CI network hiccup) while sitting
        # in the pool between checkouts.
        self.engine = create_async_engine(
            self.database_url, echo=False, future=True, pool_pre_ping=True
        )
        self._configure_sqlite()
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    # SQLite's defaults are wrong for a control plane that writes from several
    # coroutines at once, and they caused real intermittent CI failures:
    # `sqlite3.OperationalError: database is locked`, twice, in two unrelated
    # tests. Measured defaults were journal_mode=delete and busy_timeout=5000.
    #
    #   journal_mode=delete  a writer holds an exclusive lock, so it blocks
    #                        *readers* too. Both CI failures were reads racing
    #                        an orchestrator write. WAL lets readers proceed
    #                        against the last committed snapshot instead.
    #   busy_timeout=5000    a contended writer gives up after 5s. On a loaded
    #                        two-core CI runner that is not a generous budget.
    #
    # `synchronous` is deliberately left at FULL. NORMAL is the usual companion
    # to WAL and is faster, but it trades away durability on OS crash or power
    # loss, and not losing work is the one thing this project promises. WAL
    # alone is what fixes the contention; weakening fsync is not required.
    SQLITE_BUSY_TIMEOUT_MS = 30_000

    def _configure_sqlite(self) -> None:
        """Apply SQLite-only pragmas, on SQLite only.

        The rest of this module is deliberately dialect-agnostic and stays that
        way: this hangs off a connect event and is guarded by the dialect name,
        so a Postgres URL never sees it.
        """
        if self.engine.dialect.name != "sqlite":
            return

        timeout_ms = self.SQLITE_BUSY_TIMEOUT_MS

        @event.listens_for(self.engine.sync_engine, "connect")
        def _set_pragmas(dbapi_connection, _record) -> None:  # pragma: no cover - event hook
            cursor = dbapi_connection.cursor()
            try:
                # No-op for :memory: databases, which cannot use WAL — the
                # pragma simply reports the mode it kept.
                cursor.execute("PRAGMA journal_mode=WAL")
                # aether: allow-insecure — PRAGMA cannot take a bound
                # parameter, and timeout_ms is our own int constant.
                cursor.execute(f"PRAGMA busy_timeout={timeout_ms}")
            finally:
                cursor.close()

    async def create_all(self) -> None:
        # import models for metadata registration
        from aether.storage import models as _models  # noqa: F401

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
