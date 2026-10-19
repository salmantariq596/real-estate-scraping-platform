"""
app/api/v1/scraper.py — Scraper status and control endpoints.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.orm import ScrapeRun, ScraperError
from app.repositories.listing_urls import ListingURLRepository
from app.repositories.listings import ListingRepository
from app.schemas.listings import ScraperStatusResponse
from app.scraper.proxy_manager import get_live_proxy_stats

router = APIRouter(prefix="/scraper", tags=["scraper"])


@router.get("/proxy-stats")
async def get_proxy_stats():
    """Monitoring endpoint: current proxy pool health -- active provider,
    per-endpoint success/failure counts, live in-flight requests, and
    cooldown status. Read-only, no DB access. Reflects the real scraper
    worker processes (via Redis), not whichever process serves this API
    call -- see app/scraper/proxy_manager.py::get_live_proxy_stats."""
    return await get_live_proxy_stats()


@router.get("/status", response_model=ScraperStatusResponse)
async def get_scraper_status(db: AsyncSession = Depends(get_db)):
    """Return current scraper status and queue depth."""
    url_repo = ListingURLRepository(db)
    listing_repo = ListingRepository(db)

    url_counts = await url_repo.get_counts()
    stats = await listing_repo.get_statistics()

    # Last run
    result = await db.execute(
        select(ScrapeRun).order_by(ScrapeRun.started_at.desc()).limit(1)
    )
    last_run_obj = result.scalar_one_or_none()
    last_run: Dict[str, Any] = {}
    if last_run_obj:
        last_run = {
            "id": last_run_obj.id,
            "run_type": last_run_obj.run_type,
            "status": last_run_obj.status,
            "urls_discovered": last_run_obj.urls_discovered,
            "urls_scraped": last_run_obj.urls_scraped,
            "listings_created": last_run_obj.listings_created,
            "listings_updated": last_run_obj.listings_updated,
            "started_at": last_run_obj.started_at.isoformat() if last_run_obj.started_at else None,
            "finished_at": last_run_obj.finished_at.isoformat() if last_run_obj.finished_at else None,
        }

    # Recent errors (last 24h)
    from datetime import datetime, timedelta
    since = datetime.utcnow() - timedelta(hours=24)
    from sqlalchemy import func
    error_count_result = await db.execute(
        select(func.count()).select_from(ScraperError).where(
            ScraperError.created_at >= since
        )
    )
    recent_errors = error_count_result.scalar_one() or 0

    pending = url_counts.get("pending", 0)
    if pending > 0:
        status = "active" if last_run.get("status") == "running" else "pending"
    elif last_run.get("status") == "running":
        status = "running"
    else:
        status = "idle"

    return ScraperStatusResponse(
        status=status,
        pending_urls=pending,
        failed_urls=url_counts.get("failed", 0),
        total_listings=stats["total_listings"],
        last_run=last_run or None,
        recent_errors=recent_errors,
    )


@router.get("/runs")
async def list_runs(
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """List recent scrape runs."""
    result = await db.execute(
        select(ScrapeRun).order_by(ScrapeRun.started_at.desc()).limit(limit)
    )
    runs = result.scalars().all()
    return [
        {
            "id": r.id,
            "run_type": r.run_type,
            "status": r.status,
            "urls_discovered": r.urls_discovered,
            "urls_scraped": r.urls_scraped,
            "urls_failed": r.urls_failed,
            "listings_created": r.listings_created,
            "listings_updated": r.listings_updated,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "error_message": r.error_message,
        }
        for r in runs
    ]


@router.get("/errors")
async def list_errors(limit: int = 50, db: AsyncSession = Depends(get_db)):
    """List recent scraper errors."""
    result = await db.execute(
        select(ScraperError).order_by(ScraperError.created_at.desc()).limit(limit)
    )
    errors = result.scalars().all()
    return [
        {
            "id": e.id,
            "run_id": e.run_id,
            "url": e.url,
            "error_type": e.error_type,
            "error_message": e.error_message,
            "created_at": e.created_at,
        }
        for e in errors
    ]