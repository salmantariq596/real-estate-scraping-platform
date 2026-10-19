"""
app/db/session.py — Async SQLAlchemy engine + session factory.
Supports both PostgreSQL (asyncpg) and MySQL (aiomysql).
"""
from __future__ import annotations

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

settings = get_settings()

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        connect_args: dict = {}

        if settings.database_type == "postgresql":
            connect_args = {
                "server_settings": {"application_name": "pincali_etl"},
                # statement_timeout: kill any single query that runs longer than
                # 60 s. Prevents a stuck migration or runaway SELECT from holding
                # a connection open indefinitely and starving the scraper pool.
                "command_timeout": 60,
            }
        elif settings.database_type == "mysql":
            connect_args = {
                "charset": "utf8mb4",
                # MySQL kills idle connections after wait_timeout (default 8 h,
                # often 600 s on managed services). pool_recycle handles this at
                # the SQLAlchemy level; connect_timeout is the TCP handshake limit.
                "connect_timeout": 10,
            }

        # ROOT CAUSE FIX: db_pool_size / db_pool_max_overflow / db_pool_timeout /
        # db_pool_recycle were referenced here but never defined in config.py, so
        # Python raised AttributeError and SQLAlchemy silently used its own defaults
        # (pool_size=5, max_overflow=10, pool_timeout=30 s).
        #
        # On the server, FastAPI + the scraper worker share this engine. FastAPI
        # holds ~5 connections permanently, leaving only 10 for the scraper. With
        # concurrency=5 the pool exhausted after 2 batches (100 URLs), SQLAlchemy
        # waited 30 s for a free connection, raised QueuePool limit exceeded, and
        # the outer except-Exception in run_once() caught it silently — making the
        # worker appear to finish after exactly 100 listings every time.
        #
        # The fix is in config.py (proper defaults: pool_size=15, max_overflow=30,
        # timeout=60 s, recycle=1800 s). session.py now reads them correctly.
        _engine = create_async_engine(
            settings.async_database_url,
            echo=settings.api_debug,
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_pool_max_overflow,
            pool_timeout=settings.db_pool_timeout,
            pool_recycle=settings.db_pool_recycle,
            connect_args=connect_args,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields an async DB session."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None