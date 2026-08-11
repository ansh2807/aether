"""SQLAlchemy async engine and session.

Backend-agnostic by design: everything here is plain SQLAlchemy Core/ORM, no
SQLite-only pragmas, connect_args, or pool classes. `Database` is handed a
`settings.database_url` and builds an async engine for whatever dialect that
URL names — `sqlite+aiosqlite://` (the zero-setup default) or
`postgresql+asyncpg://` (opt into it with the `postgres` extra: `pip install
aether[postgres]`). See `storage/repository.py` and `storage/models.py` for
the rest of the portability story — the ORM layer has no dialect-specific
branches either.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

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
