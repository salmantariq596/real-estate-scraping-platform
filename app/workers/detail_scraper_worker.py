"""
app/workers/detail_scraper_worker.py — Consumes pending listing_urls,
scrapes property detail pages, normalizes data, and persists to DB.

PATCHED 2026-06-04:
- Proxy rotation in browser launch and restart
- Stealth browser args added
"""
from __future__ import annotations

import asyncio
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.config import get_settings
from app.core.logging import get_worker_logger
from app.db.session import get_session_factory
from app.models.orm import ScrapeRun, ScraperError
from app.repositories.listing_urls import ListingURLRepository
from app.repositories.listings import ListingRepository
from app.scraper.core import scrape_url
from app.scraper.proxy_manager import get_proxy_manager
from app.services.exchange_rate import get_mxn_to_usd_rate
from app.services.normalizer import normalize_property_record
from sqlalchemy.exc import TimeoutError as SAPoolTimeout
from sqlalchemy import select, func

settings = get_settings()
logger = get_worker_logger()

PLAYWRIGHT_AVAILABLE = False
try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    pass

# Stealth args for browser launch
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-web-security",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-site-isolation-trials",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-accelerated-2d-canvas",
    "--disable-gpu",
    "--window-size=1366,768",
]


class DetailScraperWorker:
    def __init__(self):
        self._running = False
        self._current_run_id: Optional[int] = None

    async def _process_url_batch(
        self,
        urls: List[Any],
        browser,
        exchange_rate: float,
        run_id: int,
        session_factory,
    ) -> Dict[str, int]:
        stats = {"success": 0, "failed": 0, "created": 0, "updated": 0}
        shared_state: Dict[str, Any] = {"captcha_cookies": []}

        semaphore = asyncio.Semaphore(settings.pincali_concurrency)

        async def _process_one(url_obj):
            async with semaphore:
                url_id = url_obj.id
                url = url_obj.url

                async with session_factory() as session:
                    url_repo = ListingURLRepository(session)
                    listing_repo = ListingRepository(session)

                    await url_repo.mark_scraping(url_id)
                    await session.commit()

                    data, source, error = await scrape_url(url, browser, shared_state)

                    if data is None:
                        await url_repo.mark_failed(url_id, error or "Unknown error")
                        session.add(
                            ScraperError(
                                run_id=run_id,
                                url=url,
                                error_type="scrape_error",
                                error_message=error or "Unknown error",
                            )
                        )
                        await session.commit()
                        stats["failed"] += 1
                        logger.warning(f"[Scraper] FAILED {url}: {error}")
                        return

                    try:
                        normalized = normalize_property_record(data)
                        if normalized is None:
                            raise ValueError("normalize_property_record returned None")

                        photos = normalized.pop("_photos", [])
                        videos = normalized.pop("_videos", [])
                        tours = normalized.pop("_tours", [])

                        # price_usd is computed by ListingRepository.upsert()
                        # from original_price + original_currency. Do not override here.

                        listing, created, changed = await listing_repo.upsert(
                            normalized, exchange_rate=exchange_rate
                        )
                        await listing_repo.bulk_upsert_media(listing.id, photos, videos, tours)
                        await url_repo.mark_done(url_id, external_id=str(listing.external_id))
                        await session.commit()

                        stats["success"] += 1
                        if created:
                            stats["created"] += 1
                        else:
                            stats["updated"] += 1
                        logger.info(
                            f"[Scraper] {'CREATED' if created else 'UPDATED'} "
                            f"[{source}] {url}"
                        )

                    except Exception as e:
                        tb = traceback.format_exc()
                        await url_repo.mark_failed(url_id, str(e)[:1024])
                        session.add(
                            ScraperError(
                                run_id=run_id,
                                url=url,
                                error_type="normalization_error",
                                error_message=str(e),
                                traceback=tb,
                            )
                        )
                        await session.commit()
                        stats["failed"] += 1
                        logger.error(f"[Scraper] Normalization error for {url}: {e}")

        tasks = [_process_one(u) for u in urls]
        await asyncio.gather(*tasks, return_exceptions=True)
        return stats

    async def run_once(self) -> Dict:
        """Run one scraping pass — process all pending URLs."""
        factory = get_session_factory()
        exchange_rate = await get_mxn_to_usd_rate()
        overall = {"processed": 0, "created": 0, "updated": 0, "failed": 0}

        async with factory() as session:
            url_repo = ListingURLRepository(session)

            stale = await url_repo.reset_stale_scraping()
            if stale:
                await session.commit()
                logger.info(f"[Scraper] Reset {stale} stale scraping records")

            requeued = await url_repo.requeue_stale_done(
                older_than_hours=settings.scraper_rescrape_after_hours
            )
            if requeued:
                await session.commit()
                logger.info(f"[Scraper] Re-queued {requeued} stale done URLs for re-scrape")

            retried = await url_repo.requeue_retryable_failed()
            if retried:
                await session.commit()
                logger.info(
                    f"[Scraper] Re-queued {retried} retryable failed URLs "
                    f"(failed >= {settings.pincali_failed_url_requeue_after_hours}h ago)"
                )

            # Snapshot filter breakdown from pending URLs so the runs dashboard
            # shows which discovery combos this run is processing.
            from app.models.orm import ListingURL
            filter_rows = await session.execute(
                select(ListingURL.filter_name, ListingURL.sort_method,
                        func.count().label("cnt"))
                .where(ListingURL.status == "pending")
                .group_by(ListingURL.filter_name, ListingURL.sort_method)
                .order_by(func.count().desc())
                .limit(15)
            )
            filter_breakdown = {
                f"{row.filter_name or 'unknown'}/{row.sort_method or 'newest'}": row.cnt
                for row in filter_rows
            }

            run = ScrapeRun(
                run_type="detail",
                source="pincali",
                status="running",
                started_at=datetime.utcnow(),
                meta={"filter_breakdown": filter_breakdown} if filter_breakdown else None,
            )
            session.add(run)
            await session.commit()
            await session.refresh(run)
            self._current_run_id = run.id
            logger.info(f"[Scraper] Run #{run.id} started — detail scraper run beginning")

        playwright_ctx = None
        browser = None
        proxy_manager = get_proxy_manager()
        current_proxy_endpoint = None
        if PLAYWRIGHT_AVAILABLE:
            playwright_ctx = await async_playwright().__aenter__()
            # PATCH: rotate proxy per browser launch via ProxyManager
            current_proxy_endpoint = await proxy_manager.get_proxy()
            proxy = current_proxy_endpoint.as_playwright_proxy() if current_proxy_endpoint else None
            browser = await playwright_ctx.chromium.launch(
                headless=settings.pincali_headless,
                proxy=proxy,
                args=STEALTH_ARGS,
            )

        try:
            batch_size = settings.scraper_batch_size
            browser_url_count = 0

            while True:
                async with factory() as session:
                    url_repo = ListingURLRepository(session)
                    pending = await url_repo.get_pending(limit=batch_size)
                    if not pending:
                        break

                if (
                    PLAYWRIGHT_AVAILABLE
                    and browser is not None
                    and browser_url_count >= settings.pincali_browser_restart_every
                ):
                    try:
                        await browser.close()
                    except Exception:
                        pass
                    try:
                        # PATCH: routine (non-error) restart -- the previous
                        # proxy session did its job for a full batch, so
                        # report success before rotating to a fresh one.
                        await proxy_manager.report_success(current_proxy_endpoint)
                        current_proxy_endpoint = await proxy_manager.get_proxy()
                        proxy = (
                            current_proxy_endpoint.as_playwright_proxy()
                            if current_proxy_endpoint
                            else None
                        )
                        browser = await playwright_ctx.chromium.launch(
                            headless=settings.pincali_headless,
                            proxy=proxy,
                            args=STEALTH_ARGS,
                        )
                        browser_url_count = 0
                        logger.info(
                            f"[Scraper] Browser restarted "
                            f"(proxy={current_proxy_endpoint.proxy_id if current_proxy_endpoint else 'none'})"
                        )
                    except Exception as restart_err:
                        # PATCH: an error-triggered restart likely means the
                        # proxy session itself is bad -- report the failure
                        # so ProxyManager can cool it down before next pick.
                        await proxy_manager.report_failure(
                            current_proxy_endpoint, reason=f"browser_restart_failed: {restart_err}"
                        )
                        logger.error(
                            f"[Scraper] Browser restart failed: {restart_err} — "
                            "continuing in HTTP-only mode"
                        )
                        browser = None
                        browser_url_count = 0

                stats = await self._process_url_batch(
                    pending, browser, exchange_rate, run.id, factory
                )
                overall["processed"] += len(pending)
                overall["created"] += stats["created"]
                overall["updated"] += stats["updated"]
                overall["failed"] += stats["failed"]
                browser_url_count += len(pending)

                # ── Live stats flush ──────────────────────────────────────────
                # Write running totals to DB after every batch so the dashboard
                # shows real-time progress instead of all-zeros until run ends.
                try:
                    async with factory() as live_session:
                        live_result = await live_session.execute(
                            select(ScrapeRun).where(ScrapeRun.id == run.id)
                        )
                        live_run = live_result.scalar_one_or_none()
                        if live_run:
                            live_run.urls_scraped = overall["processed"]
                            live_run.urls_failed = overall["failed"]
                            live_run.listings_created = overall["created"]
                            live_run.listings_updated = overall["updated"]
                            await live_session.commit()
                except Exception as flush_err:
                    logger.debug(f"[Scraper] Live stats flush failed (non-fatal): {flush_err}")

                logger.info(
                    f"[Scraper] Batch complete: "
                    f"+{stats['created']} new, "
                    f"~{stats['updated']} updated, "
                    f"x{stats['failed']} failed"
                )

        except SAPoolTimeout as e:
            logger.error(
                f"[Scraper] DB connection pool exhausted after "
                f"{overall['processed']} URLs — increase DB_POOL_SIZE or "
                f"DB_POOL_MAX_OVERFLOW in .env. Error: {e}",
                exc_info=True,
            )
            overall["error"] = (
                f"DB pool exhausted after {overall['processed']} URLs: {e}. "
                "Fix: raise DB_POOL_SIZE / DB_POOL_MAX_OVERFLOW in .env."
            )
        except Exception as e:
            logger.error(f"[Scraper] Worker error: {e}", exc_info=True)
            overall["error"] = str(e)
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if playwright_ctx:
                try:
                    await playwright_ctx.__aexit__(None, None, None)
                except Exception:
                    pass

        async with factory() as session:
            result = await session.execute(
                select(ScrapeRun).where(ScrapeRun.id == run.id)
            )
            final_run = result.scalar_one_or_none()
            if final_run:
                final_run.status = "completed" if not overall.get("error") else "failed"
                final_run.finished_at = datetime.utcnow()
                final_run.urls_scraped = overall.get("processed", 0)
                final_run.urls_failed = overall.get("failed", 0)
                final_run.listings_created = overall.get("created", 0)
                final_run.listings_updated = overall.get("updated", 0)
                if overall.get("error"):
                    final_run.error_message = overall["error"]
                await session.commit()

        logger.info(
            f"[Scraper] Run #{run.id} finished | "
            f"processed={overall['processed']} created={overall['created']} "
            f"updated={overall['updated']} failed={overall['failed']}"
        )

        overall["status"] = "completed"
        return overall

    async def run_loop(self) -> None:
        """Continuously poll for pending URLs and scrape them."""
        self._running = True
        logger.info("[Scraper] Detail worker started")
        while self._running:
            try:
                async with get_session_factory()() as session:
                    url_repo = ListingURLRepository(session)
                    counts = await url_repo.get_counts()

                pending = counts.get("pending", 0)
                # Only trigger a new run when there are URLs actually waiting to be scraped.
                # The previous condition (pending > 0 or done > 0) caused run_once() to be
                # called on every poll cycle even when pending=0 (because done is always >0
                # after the first run), flooding the DB with ScrapeRun rows of all-zero counters.
                has_work = pending > 0

                if has_work:
                    logger.info(
                        f"[Scraper] {pending} pending URLs — starting scrape run"
                    )
                    stats = await self.run_once()
                    logger.info(f"[Scraper] Run complete: {stats}")
                else:
                    logger.debug("[Scraper] No URLs in queue, sleeping...")

            except Exception as e:
                logger.error(f"[Scraper] Loop error: {e}", exc_info=True)

            await asyncio.sleep(settings.scraper_poll_interval)

    def stop(self) -> None:
        self._running = False


async def main():
    worker = DetailScraperWorker()
    await worker.run_once()


if __name__ == "__main__":
    asyncio.run(main())