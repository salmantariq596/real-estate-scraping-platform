"""
Shared pytest fixtures for the Pincali ETL platform test suite.
"""
from __future__ import annotations

import asyncio
import os
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

# Force SQLite in-memory for all tests
os.environ.setdefault("DATABASE_TYPE", "postgresql")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5432")
os.environ.setdefault("POSTGRES_DB", "pincali_test")
os.environ.setdefault("POSTGRES_USER", "pincali")
os.environ.setdefault("POSTGRES_PASSWORD", "changeme")
os.environ.setdefault("MXN_TO_USD_RATE", "20.0")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

# Use SQLite for tests (no external DB required)
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

from app.models.orm import Base
from app.db.session import get_db
from app.main import app


@pytest.fixture(scope="session")
def event_loop():
    """Create an event loop for the test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="function")
async def db_engine():
    """Create an in-memory SQLite engine for each test function."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    """Provide a clean database session per test."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def client(db_session) -> AsyncGenerator[AsyncClient, None]:
    """Provide a test HTTP client with DB dependency overridden."""

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac

    app.dependency_overrides.clear()


# ── Seed helpers ──────────────────────────────────────────────────────────────

def make_listing_data(**overrides) -> dict:
    """Return a minimal valid listing payload for tests."""
    base = {
        "external_id": "test-001",
        "source": "pincali",
        "origin_url": "https://www.pincali.com/en/home/test-001",
        "name": "Test Property",
        "price_usd": 250000.0,
        "original_price": 5000000.0,
        "original_currency": "MXN",
        "mls_status": "Active",
        "property_type": "B",
        "city": "cabo san lucas",
        "country": "Mexico",
        "beds_total": 3,
        "baths_full": 2,
    }
    base.update(overrides)
    return base
