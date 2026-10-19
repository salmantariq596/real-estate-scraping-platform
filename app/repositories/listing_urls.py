"""
app/repositories/listing_urls.py — DB access for the URL discovery queue.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select, update, and_, case
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.orm import ListingURL
from app.core.config import get_settings

settings = get_settings()


class ListingURLRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_pending(self, limit: int = 100) -> List[ListingURL]:
        result = await self.session.execute(
            select(ListingURL)
            .where(ListingURL.status == "pending")
            .order_by(
                case((ListingURL.last_scraped_at.is_(None), 0), else_=1),
                ListingURL.discovered_at.asc(),
            )
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_failed(self, limit: int = 100) -> List[ListingURL]:
        result = await self.session.execute(
            select(ListingURL)
            .where(
                and_(
                    ListingURL.status == "failed",
                    ListingURL.scrape_attempts < settings.pincali_max_retries_per_url + 1,
                )
            )
            .order_by(ListingURL.updated_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def bulk_insert_discovered(self, urls: List[Dict]) -> int:
        """
        Insert discovered URLs, ignoring duplicates.
        Returns count of newly inserted rows.
        """
        if not urls:
            return 0

        inserted = 0
        for url_data in urls:
            existing = await self.session.execute(
                select(ListingURL).where(ListingURL.url == url_data["url"])
            )
            if existing.scalar_one_or_none() is None:
                self.session.add(
                    ListingURL(
                        url=url_data["url"],
                        source=url_data.get("source", "pincali"),
                        filter_name=url_data.get("filter_name"),
                        filter_id=url_data.get("filter_id"),
                        sort_method=url_data.get("sort_method"),
                        status="pending",
                    )
                )
                inserted += 1

        await self.session.flush()
        return inserted

    async def mark_scraping(self, url_id: int) -> None:
        await self.session.execute(
            update(ListingURL)
            .where(ListingURL.id == url_id)
            .values(
                status="scraping",
                updated_at=datetime.utcnow(),
            )
        )

    async def mark_done(self, url_id: int, external_id: Optional[str] = None) -> None:
        values: Dict = {
            "status": "done",
            "last_scraped_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "error_message": None,
        }
        if external_id:
            values["external_id"] = external_id
        await self.session.execute(
            update(ListingURL).where(ListingURL.id == url_id).values(**values)
        )

    async def mark_failed(self, url_id: int, error: str) -> None:
        await self.session.execute(
            update(ListingURL)
            .where(ListingURL.id == url_id)
            .values(
                status="failed",
                error_message=error[:1024],
                scrape_attempts=ListingURL.scrape_attempts + 1,
                updated_at=datetime.utcnow(),
            )
        )

    async def reset_stale_scraping(self) -> int:
        """Reset URLs stuck in 'scraping' state (e.g. after crash)."""
        result = await self.session.execute(
            update(ListingURL)
            .where(ListingURL.status == "scraping")
            .values(status="pending", updated_at=datetime.utcnow())
        )
        return result.rowcount

    # ── BUG FIX #1: Re-queue done URLs that haven't been scraped recently ──────
    async def requeue_stale_done(self, older_than_hours: int = 24) -> int:
        """
        Re-queue URLs that were previously scraped but are now stale.
        This is what drives the '~N updated' column on the dashboard —
        without this, every URL stays 'done' forever and the detail worker
        finds nothing to do after the very first pass.
        """
        cutoff = datetime.utcnow() - timedelta(hours=older_than_hours)
        result = await self.session.execute(
            update(ListingURL)
            .where(
                and_(
                    ListingURL.status == "done",
                    ListingURL.last_scraped_at < cutoff,
                )
            )
            .values(status="pending", updated_at=datetime.utcnow())
        )
        return result.rowcount

    # ── BUG FIX #1b: Also re-queue failed URLs that are eligible for retry ─────
    async def requeue_retryable_failed(
        self, after_hours: Optional[int] = None
    ) -> int:
        """
        Reset failed URLs that still have retry budget back to pending --
        but only once they've sat failed for at least `after_hours`
        (default: settings.pincali_failed_url_requeue_after_hours, 12h).

        Called at the start of each detail-scraper pass. The cooldown
        prevents a persistently-blocked URL from being retried on every
        single pass (which could be minutes apart) -- it gives whatever
        caused the failure (a bad proxy, a temporary site-side block, a
        transient network error) real time to clear before we spend
        another retry attempt on it.
        """
        cooldown_hours = (
            after_hours
            if after_hours is not None
            else settings.pincali_failed_url_requeue_after_hours
        )
        cutoff = datetime.utcnow() - timedelta(hours=cooldown_hours)
        result = await self.session.execute(
            update(ListingURL)
            .where(
                and_(
                    ListingURL.status == "failed",
                    ListingURL.scrape_attempts <= settings.pincali_max_retries_per_url,
                    ListingURL.updated_at < cutoff,
                )
            )
            .values(status="pending", updated_at=datetime.utcnow())
        )
        return result.rowcount

    async def get_failed_requeue_eta(self) -> Dict[str, int]:
        """
        Diagnostic helper for the dashboard: how many failed-with-budget
        URLs are waiting on the 12h cooldown vs already eligible right now.
        """
        cutoff = datetime.utcnow() - timedelta(
            hours=settings.pincali_failed_url_requeue_after_hours
        )
        eligible_now = await self.session.scalar(
            select(func.count()).select_from(ListingURL).where(
                and_(
                    ListingURL.status == "failed",
                    ListingURL.scrape_attempts <= settings.pincali_max_retries_per_url,
                    ListingURL.updated_at < cutoff,
                )
            )
        ) or 0
        still_cooling_down = await self.session.scalar(
            select(func.count()).select_from(ListingURL).where(
                and_(
                    ListingURL.status == "failed",
                    ListingURL.scrape_attempts <= settings.pincali_max_retries_per_url,
                    ListingURL.updated_at >= cutoff,
                )
            )
        ) or 0
        retry_budget_exhausted = await self.session.scalar(
            select(func.count()).select_from(ListingURL).where(
                and_(
                    ListingURL.status == "failed",
                    ListingURL.scrape_attempts > settings.pincali_max_retries_per_url,
                )
            )
        ) or 0
        return {
            "eligible_now": eligible_now,
            "still_cooling_down": still_cooling_down,
            "retry_budget_exhausted": retry_budget_exhausted,
        }

    async def get_counts(self) -> Dict[str, int]:
        rows = await self.session.execute(
            select(ListingURL.status, func.count())
            .group_by(ListingURL.status)
        )
        return {row[0]: row[1] for row in rows}

    async def url_exists(self, url: str) -> bool:
        result = await self.session.execute(
            select(func.count()).select_from(ListingURL).where(ListingURL.url == url)
        )
        return (result.scalar_one() or 0) > 0