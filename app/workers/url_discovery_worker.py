"""
app/workers/url_discovery_worker.py — Background worker that discovers
property listing URLs from Pincali and queues them for detail scraping.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_worker_logger
from app.db.session import get_session_factory
from app.models.orm import ScrapeRun, ScraperError
from app.repositories.listing_urls import ListingURLRepository
from app.scraper.discovery import DiscoveryService, generate_discovery_filters

settings = get_settings()
logger = get_worker_logger()


class URLDiscoveryWorker:
    def __init__(self):
        self._running = False
        self._current_run_id: Optional[int] = None

    async def _create_run(self, session: AsyncSession) -> ScrapeRun:
        run = ScrapeRun(
            run_type="discovery",
            source="pincali",
            status="running",
            started_at=datetime.utcnow(),
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run

    async def _finish_run(
        self,
        session: AsyncSession,
        run: ScrapeRun,
        status: str = "completed",
        error: Optional[str] = None,
    ) -> None:
        run.status = status
        run.finished_at = datetime.utcnow()
        if error:
            run.error_message = error
        await session.commit()

    async def run_once(self) -> Dict:
        """Run a single discovery pass. Returns summary stats."""
        factory = get_session_factory()
        stats = {
            "urls_discovered": 0,
            "filters_processed": 0,
            "started_at": datetime.utcnow().isoformat(),
        }

        async with factory() as session:
            run = await self._create_run(session)
            self._current_run_id = run.id
            url_repo = ListingURLRepository(session)

            # Existing URLs to avoid re-queuing
            existing_counts = await url_repo.get_counts()
            logger.info(
                f"[Discovery] Starting run #{run.id}. "
                f"Existing: {existing_counts}"
            )

            try:
                filters = generate_discovery_filters()
                filter_combos: Dict[str, int] = {}   # filter_name → urls found

                async with DiscoveryService(
                    max_pages_per_filter=settings.discovery_max_pages_per_filter,
                    request_delay=0.8,
                ) as svc:
                    async for batch in svc.discover_all(filters):
                        url_data = [
                            {
                                "url": u,
                                "source": "pincali",
                                "filter_name": batch["filter_name"],
                                "filter_id": batch["filter_id"],
                                "sort_method": "newest",
                            }
                            for u in batch["urls"]
                        ]
                        inserted = await url_repo.bulk_insert_discovered(url_data)

                        run.urls_discovered += inserted
                        stats["urls_discovered"] += inserted
                        stats["filters_processed"] = batch["filter_id"]

                        # Track per-filter URL counts in meta for the runs dashboard
                        key = batch["filter_name"] or "unknown"
                        filter_combos[key] = filter_combos.get(key, 0) + inserted
                        run.meta = {
                            "filter_combos": filter_combos,
                            "current_filter": batch["filter_name"],
                            "current_page": batch.get("page"),
                        }

                        await session.commit()
                        logger.info(
                            f"[Discovery] Filter {batch['filter_id']} page {batch['page']}: "
                            f"+{inserted} new URLs"
                        )

                await self._finish_run(session, run, "completed")
                stats["status"] = "completed"

            except Exception as e:
                logger.error(f"[Discovery] Run #{run.id} failed: {e}", exc_info=True)
                session.add(
                    ScraperError(
                        run_id=run.id,
                        error_type="discovery_error",
                        error_message=str(e),
                    )
                )
                await self._finish_run(session, run, "failed", str(e))
                stats["status"] = "failed"
                stats["error"] = str(e)

        return stats

    async def run_loop(self) -> None:
        """Continuously discover URLs at the configured interval."""
        self._running = True
        logger.info(
            f"[Discovery] Worker started. Interval: {settings.discovery_interval_seconds}s"
        )
        while self._running:
            try:
                stats = await self.run_once()
                logger.info(f"[Discovery] Run complete: {stats}")
            except Exception as e:
                logger.error(f"[Discovery] Worker error: {e}", exc_info=True)
            await asyncio.sleep(settings.discovery_interval_seconds)

    def stop(self) -> None:
        self._running = False


# Allow running directly
async def main():
    worker = URLDiscoveryWorker()
    await worker.run_once()


if __name__ == "__main__":
    asyncio.run(main())