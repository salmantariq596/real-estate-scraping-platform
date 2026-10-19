"""
tests/test_failed_url_requeue.py — Regression tests for the 12h
auto-requeue cooldown on failed URLs (ListingURLRepository).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.models.orm import ListingURL
from app.repositories.listing_urls import ListingURLRepository
from sqlalchemy import select


async def _seed(db_session, id_, url, status, scrape_attempts, updated_at):
    row = ListingURL(
        id=id_, url=url, status=status,
        scrape_attempts=scrape_attempts, updated_at=updated_at,
    )
    await db_session.merge(row)
    await db_session.commit()


@pytest.mark.asyncio
async def test_recently_failed_url_is_not_requeued_yet(db_session):
    now = datetime.utcnow()
    await _seed(db_session, 1, "https://x.com/recent", "failed", 1, now - timedelta(hours=1))

    repo = ListingURLRepository(db_session)
    count = await repo.requeue_retryable_failed()
    await db_session.commit()

    assert count == 0
    row = (await db_session.execute(select(ListingURL).where(ListingURL.id == 1))).scalar_one()
    assert row.status == "failed"


@pytest.mark.asyncio
async def test_url_failed_over_12h_ago_with_retry_budget_is_requeued(db_session):
    now = datetime.utcnow()
    await _seed(db_session, 2, "https://x.com/old", "failed", 1, now - timedelta(hours=13))

    repo = ListingURLRepository(db_session)
    count = await repo.requeue_retryable_failed()
    await db_session.commit()

    assert count == 1
    row = (await db_session.execute(select(ListingURL).where(ListingURL.id == 2))).scalar_one()
    assert row.status == "pending"


@pytest.mark.asyncio
async def test_exhausted_retry_budget_is_never_requeued_even_after_cooldown(db_session):
    now = datetime.utcnow()
    await _seed(db_session, 3, "https://x.com/exhausted", "failed", 5, now - timedelta(hours=48))

    repo = ListingURLRepository(db_session)
    count = await repo.requeue_retryable_failed()
    await db_session.commit()

    assert count == 0
    row = (await db_session.execute(select(ListingURL).where(ListingURL.id == 3))).scalar_one()
    assert row.status == "failed"


@pytest.mark.asyncio
async def test_custom_after_hours_override(db_session):
    now = datetime.utcnow()
    await _seed(db_session, 4, "https://x.com/two-hours-old", "failed", 1, now - timedelta(hours=2))

    repo = ListingURLRepository(db_session)
    # With a 1h cooldown override, a 2h-old failure should already be eligible.
    count = await repo.requeue_retryable_failed(after_hours=1)
    await db_session.commit()

    assert count == 1


@pytest.mark.asyncio
async def test_get_failed_requeue_eta_buckets_correctly(db_session):
    now = datetime.utcnow()
    await _seed(db_session, 5, "https://x.com/a", "failed", 1, now - timedelta(hours=1))   # cooling down
    await _seed(db_session, 6, "https://x.com/b", "failed", 1, now - timedelta(hours=13))  # eligible now
    await _seed(db_session, 7, "https://x.com/c", "failed", 5, now - timedelta(hours=20))  # exhausted

    repo = ListingURLRepository(db_session)
    eta = await repo.get_failed_requeue_eta()

    assert eta["still_cooling_down"] == 1
    assert eta["eligible_now"] == 1
    assert eta["retry_budget_exhausted"] == 1
