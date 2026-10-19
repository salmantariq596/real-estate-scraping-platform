"""
Tests for the database repositories and ORM models.
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.orm import Listing, ListingURL
from app.repositories.listings import ListingRepository
from app.repositories.listing_urls import ListingURLRepository
from app.schemas.listings import ListingFilters
from tests.conftest import make_listing_data


@pytest.mark.asyncio
async def test_listing_upsert_insert(db_session: AsyncSession):
    """Upserting a new listing inserts a row."""
    repo = ListingRepository(db_session)
    data = make_listing_data()
    listing, created, _ = await repo.upsert(data, exchange_rate=20.0)
    assert created is True
    assert listing.id is not None
    assert listing.external_id == "test-001"
    assert listing.price_usd == 250000.0


@pytest.mark.asyncio
async def test_listing_upsert_update(db_session: AsyncSession):
    """Upserting the same external_id+source updates the existing row."""
    repo = ListingRepository(db_session)
    data = make_listing_data()
    listing1, _, _ = await repo.upsert(data, exchange_rate=20.0)

    updated = make_listing_data(original_price=6_000_000.0, name="Updated Property")
    listing2, created, changed = await repo.upsert(updated, exchange_rate=20.0)

    assert created is False
    assert "price_usd" in changed  # tracked fields only
    assert listing1.id == listing2.id
    assert listing2.price_usd == 300000.0
    assert listing2.name == "Updated Property"


@pytest.mark.asyncio
async def test_listing_get_by_id(db_session: AsyncSession):
    """get_by_id returns the correct listing."""
    repo = ListingRepository(db_session)
    created, _, _ = await repo.upsert(make_listing_data())
    fetched = await repo.get_by_id(created.id)
    assert fetched is not None
    assert fetched.external_id == created.external_id


@pytest.mark.asyncio
async def test_listing_get_by_id_not_found(db_session: AsyncSession):
    """get_by_id returns None for a missing ID."""
    repo = ListingRepository(db_session)
    result = await repo.get_by_id(9999999)
    assert result is None


@pytest.mark.asyncio
async def test_listing_list_paginated(db_session: AsyncSession):
    """list_paginated returns all inserted listings."""
    repo = ListingRepository(db_session)
    for i in range(5):
        await repo.upsert(make_listing_data(external_id=f"test-{i:03d}"))

    items, total = await repo.list_paginated(ListingFilters(page=1, page_size=10))
    assert total == 5
    assert len(items) == 5


@pytest.mark.asyncio
async def test_listing_list_paginated_page2(db_session: AsyncSession):
    """list_paginated respects page and page_size."""
    repo = ListingRepository(db_session)
    for i in range(7):
        await repo.upsert(make_listing_data(external_id=f"page-{i:03d}"))

    items, total = await repo.list_paginated(ListingFilters(page=2, page_size=3))
    assert total == 7
    assert len(items) == 3


@pytest.mark.asyncio
async def test_listing_statistics(db_session: AsyncSession):
    """statistics() returns expected keys and correct counts."""
    repo = ListingRepository(db_session)
    await repo.upsert(make_listing_data(mls_status="Active"))
    await repo.upsert(make_listing_data(external_id="test-002", mls_status="Sold"))

    stats = await repo.get_statistics()
    assert stats["total_listings"] == 2
    assert stats["active_listings"] == 1


@pytest.mark.asyncio
async def test_listing_url_bulk_insert(db_session: AsyncSession):
    """bulk_insert creates URL rows and deduplicates."""
    repo = ListingURLRepository(db_session)
    urls = [
        {"url": "https://pincali.com/en/home/1", "external_id": "1", "source": "pincali", "filter_name": "test"},
        {"url": "https://pincali.com/en/home/2", "external_id": "2", "source": "pincali", "filter_name": "test"},
        # Duplicate
        {"url": "https://pincali.com/en/home/1", "external_id": "1", "source": "pincali", "filter_name": "test"},
    ]
    inserted = await repo.bulk_insert_discovered(urls)
    assert inserted == 2  # duplicate silently skipped


@pytest.mark.asyncio
async def test_listing_url_get_pending(db_session: AsyncSession):
    """get_pending returns only pending URLs."""
    url_repo = ListingURLRepository(db_session)
    await url_repo.bulk_insert_discovered([
        {"url": "https://pincali.com/en/home/10", "external_id": "10", "source": "pincali", "filter_name": "t"},
        {"url": "https://pincali.com/en/home/11", "external_id": "11", "source": "pincali", "filter_name": "t"},
    ])
    pending = await url_repo.get_pending(limit=10)
    assert len(pending) == 2

    # Mark one done
    await url_repo.mark_done(pending[0].id)
    still_pending = await url_repo.get_pending(limit=10)
    assert len(still_pending) == 1


@pytest.mark.asyncio
async def test_listing_url_mark_failed(db_session: AsyncSession):
    """mark_failed records the error message."""
    url_repo = ListingURLRepository(db_session)
    await url_repo.bulk_insert_discovered([
        {"url": "https://pincali.com/en/home/20", "external_id": "20", "source": "pincali", "filter_name": "t"},
    ])
    rows = await url_repo.get_pending(limit=1)
    row = rows[0]

    await url_repo.mark_failed(row.id, "Timeout error")
    # Pending list should now be empty (status = failed)
    still_pending = await url_repo.get_pending(limit=10)
    assert len(still_pending) == 0
