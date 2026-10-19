"""
scripts/run_capped_test_batch.py — Run a CAPPED, controlled batch of detail
pages through the real scraping pipeline (same scrape_url(), same
ProxyManager, same DB writes as production) so you can measure real
bandwidth-per-URL from Webshare's own dashboard, instead of guessing.

WHY A SEPARATE SCRIPT INSTEAD OF JUST WATCHING THE WORKER:
The normal detail_scraper_worker container runs continuously and drains
the ENTIRE pending queue (tens of thousands of URLs) with no built-in
stopping point -- exactly what burned through the first 1GB. This script
processes exactly N URLs (default 500) and then stops automatically, so
you get a clean, bounded measurement window.

USAGE (see the full step-by-step procedure in the chat response):

    # 1. Stop the continuously-running workers so nothing else consumes
    #    bandwidth during the measurement window:
    docker compose stop url_discovery_worker detail_scraper_worker

    # 2. Note Webshare's "bandwidth used" number from their dashboard now.

    # 3. Run this script (from inside the api container, so it has the
    #    same DB/Redis/network access as production):
    docker compose exec api python scripts/run_capped_test_batch.py --count 500

    # 4. Note Webshare's "bandwidth used" number again. The delta divided
    #    by the "succeeded" count this script prints = real MB per
    #    successful page. (Use "succeeded", not "processed" -- failed
    #    attempts use proxy bandwidth too but produce no usable data, so
    #    counting them would UNDERSTATE your real per-page cost.)

    # 5. Restart the normal workers:
    docker compose start url_discovery_worker detail_scraper_worker

Rows processed here are REAL production work (written to the DB exactly
like the normal worker would) -- nothing here is throwaway/wasted.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# PATCH: running this as `python scripts/run_capped_test_batch.py` (rather
# than `python -m scripts.run_capped_test_batch`) only puts this script's
# own folder (/app/scripts) on sys.path, not /app itself -- so `import app`
# fails with ModuleNotFoundError even though the container's WORKDIR is
# /app. Every other entrypoint in this project is launched with -m (see
# the "found in sys.modules" RuntimeWarning from url_discovery_worker/
# detail_scraper_worker in the docker logs -- that's the runpy -m
# machinery), which sidesteps this. Making the script self-locating here
# means `docker compose exec api python scripts/run_capped_test_batch.py`
# works exactly as documented, with no change needed to how it's invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings
from app.core.logging import get_scraper_logger
from app.db.session import get_session_factory
from app.models.orm import ListingURL, ScrapeRun, ScraperError
from app.repositories.listing_urls import ListingURLRepository
from app.repositories.listings import ListingRepository
from app.scraper.core import scrape_url, PLAYWRIGHT_AVAILABLE
from app.workers.detail_scraper_worker import STEALTH_ARGS
from app.services.normalizer import normalize_property_record
from app.scraper.proxy_manager import get_proxy_manager
from app.services.exchange_rate import get_mxn_to_usd_rate

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None

settings = get_settings()
logger = get_scraper_logger()


async def _process_one(url_obj, browser, exchange_rate, run_id, session_factory, stats, shared_state):
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
            session.add(ScraperError(
                run_id=run_id, url=url, error_type="scrape_error",
                error_message=error or "Unknown error",
            ))
            await session.commit()
            stats["failed"] += 1
            print(f"  FAILED  {url} -- {error}")
            return

        try:
            normalized = normalize_property_record(data)
            if normalized is None:
                raise ValueError("normalize_property_record returned None")
            photos = normalized.pop("_photos", [])
            videos = normalized.pop("_videos", [])
            tours = normalized.pop("_tours", [])
            listing, created, _ = await listing_repo.upsert(normalized, exchange_rate=exchange_rate)
            await listing_repo.bulk_upsert_media(listing.id, photos, videos, tours)
            await url_repo.mark_done(url_id, external_id=str(listing.external_id))
            await session.commit()
            stats["succeeded"] += 1
            print(f"  OK      [{source}] {url}")
        except Exception as e:
            tb = traceback.format_exc()
            await url_repo.mark_failed(url_id, str(e)[:1024])
            session.add(ScraperError(
                run_id=run_id, url=url, error_type="normalization_error",
                error_message=str(e), traceback=tb,
            ))
            await session.commit()
            stats["failed"] += 1
            print(f"  FAILED  {url} -- normalization error: {e}")


async def main(count: int, concurrency: int):
    factory = get_session_factory()
    exchange_rate = await get_mxn_to_usd_rate()
    stats = {"succeeded": 0, "failed": 0}
    shared_state = {"captcha_cookies": []}

    async with factory() as session:
        run = ScrapeRun(
            run_type="detail",
            source="pincali",
            status="running",
            started_at=datetime.utcnow(),
            meta={"note": f"CAPPED TEST BATCH -- bandwidth measurement, count={count}"},
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

    print(f"=== Capped test batch starting -- target {count} URLs, concurrency {concurrency} ===")
    print(f"Run #{run_id}. Check Webshare's dashboard bandwidth-used number NOW, before proceeding.\n")

    playwright_ctx = None
    browser = None
    if PLAYWRIGHT_AVAILABLE and async_playwright is not None:
        playwright_ctx = await async_playwright().__aenter__()
        proxy_manager = get_proxy_manager()
        endpoint = await proxy_manager.get_proxy()
        proxy = endpoint.as_playwright_proxy() if endpoint else None
        browser = await playwright_ctx.chromium.launch(
            headless=settings.pincali_headless, proxy=proxy, args=STEALTH_ARGS,
        )

    start = time.time()
    processed = 0
    try:
        while processed < count:
            remaining = count - processed
            chunk_size = min(settings.scraper_batch_size, remaining)

            async with factory() as session:
                url_repo = ListingURLRepository(session)
                pending = await url_repo.get_pending(limit=chunk_size)

            if not pending:
                print(f"\nQueue ran dry after {processed} URLs (fewer than {count} were pending).")
                break

            semaphore = asyncio.Semaphore(concurrency)

            async def _bounded(u):
                async with semaphore:
                    await _process_one(u, browser, exchange_rate, run_id, factory, stats, shared_state)

            await asyncio.gather(*[_bounded(u) for u in pending], return_exceptions=True)
            processed += len(pending)
            print(f"--- progress: {processed}/{count} ---")

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

        elapsed = time.time() - start
        async with factory() as session:
            run_obj = await session.get(ScrapeRun, run_id)
            if run_obj:
                run_obj.status = "completed"
                run_obj.finished_at = datetime.utcnow()
                await session.commit()

        print(f"\n=== Capped test batch finished in {elapsed:.0f}s ===")
        print(f"  processed: {processed}")
        print(f"  succeeded: {stats['succeeded']}")
        print(f"  failed:    {stats['failed']}")
        print(f"\nNext: check Webshare's dashboard bandwidth-used number again.")
        print(f"  MB per successful page = (bandwidth_used_after - bandwidth_used_before) / {max(stats['succeeded'], 1)}")
        print(f"  Then: monthly_MB_needed = MB_per_page * expected_monthly_page_count * safety_margin (suggest 1.5-2x)")
        print(f"\nRemember to restart the normal workers:")
        print(f"  docker compose start url_discovery_worker detail_scraper_worker")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a capped test batch for bandwidth measurement")
    parser.add_argument("--count", type=int, default=500, help="Number of URLs to process (default 500)")
    parser.add_argument("--concurrency", type=int, default=None, help="Override PINCALI_CONCURRENCY for this run")
    args = parser.parse_args()
    concurrency = args.concurrency or settings.pincali_concurrency
    asyncio.run(main(args.count, concurrency))
