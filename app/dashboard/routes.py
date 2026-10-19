"""
app/dashboard/routes.py — Admin dashboard routes using Jinja2 templates.
"""
from __future__ import annotations

import math
from typing import Optional

from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.models.orm import Listing, ScrapeRun, ScraperError
from app.repositories.listings import ListingRepository
from app.repositories.listing_urls import ListingURLRepository
from app.scraper.proxy_manager import get_proxy_manager, get_cluster_proxy_snapshots, get_live_proxy_stats
from app.scraper.core import domain_throttler, get_cluster_throttle_snapshots
from app.core.config import get_settings
from pathlib import Path

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
settings = get_settings()


def _format_number(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _property_type_label(code) -> str:
    mapping = {
        "A": "Condo/Apt",
        "B": "House",
        "E": "Land/Lot",
        "F": "Commercial",
        "H": "Fractional",
        "I": "Multi-Family",
    }
    return mapping.get(str(code or ""), code or "—")


templates.env.filters["format_number"] = _format_number
templates.env.filters["type_label"] = _property_type_label

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    repo = ListingRepository(db)
    url_repo = ListingURLRepository(db)

    stats_data = await repo.get_statistics()
    url_counts = await url_repo.get_counts()

    # Recent listings with photos
    result = await db.execute(
        select(Listing)
        .options(selectinload(Listing.photos))
        .order_by(Listing.created_at.desc())
        .limit(10)
    )
    recent_listings = result.scalars().all()

    # Recent runs — last 8
    result2 = await db.execute(
        select(ScrapeRun).order_by(ScrapeRun.started_at.desc()).limit(8)
    )
    recent_runs = result2.scalars().all()

    # Recent errors count (last 24h)
    from datetime import datetime, timedelta
    since = datetime.utcnow() - timedelta(hours=24)
    err_count = await db.scalar(
        select(func.count()).select_from(ScraperError).where(ScraperError.created_at >= since)
    ) or 0

    combined_stats = {
        **stats_data,
        "pending_urls": url_counts.get("pending", 0),
        "failed_urls": url_counts.get("failed", 0),
        "scraped_urls": url_counts.get("done", 0),
        "recent_errors_24h": err_count,
    }

    # Proxy + throttling summary (compact -- full detail lives on /admin/proxies)
    proxy_stats = await get_live_proxy_stats()
    healthy_count = sum(1 for e in proxy_stats["endpoints"] if not e["in_cooldown"])
    cooldown_count = proxy_stats["pool_size"] - healthy_count
    requeue_eta = await url_repo.get_failed_requeue_eta()

    proxy_summary = {
        "provider": proxy_stats["provider"],
        "pool_size": proxy_stats["pool_size"],
        "healthy_count": healthy_count,
        "cooldown_count": cooldown_count,
        "requeue_eligible_now": requeue_eta["eligible_now"],
        "requeue_cooling_down": requeue_eta["still_cooling_down"],
        "quota_exceeded": proxy_stats.get("quota_exceeded", False),
        "quota_exceeded_reason": proxy_stats.get("quota_exceeded_reason", ""),
        "quota_exceeded_ago_s": proxy_stats.get("quota_exceeded_ago_s"),
    }

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "active": "dashboard",
            "stats": combined_stats,
            "recent_listings": recent_listings,
            "recent_runs": recent_runs,
            "proxy_summary": proxy_summary,
        },
    )


@router.get("/listings", response_class=HTMLResponse)
async def admin_listings(
    request: Request,
    page: int = Query(1, ge=1),
    q: Optional[str] = Query(None),
    mls_status: Optional[str] = Query(None),
    property_type: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    page_size = 30
    repo = ListingRepository(db)

    from app.schemas.listings import ListingFilters

    filters = ListingFilters(
        city=q,
        mls_status=mls_status,
        property_type=property_type,
        page=page,
        page_size=page_size,
    )
    listings, total = await repo.list_paginated(filters)
    pages = math.ceil(total / page_size) if page_size else 1

    return templates.TemplateResponse(
        "listings.html",
        {
            "request": request,
            "active": "listings",
            "listings": listings,
            "total": total,
            "page": page,
            "pages": pages,
            "q": q,
            "status_filter": mls_status,
            "type_filter": property_type,
        },
    )


@router.get("/listings/{listing_id}", response_class=HTMLResponse)
async def admin_listing_detail(
    request: Request, listing_id: int, db: AsyncSession = Depends(get_db)
):
    # BUG FIX: was falling back to dashboard.html with stub data.
    # Now uses a dedicated listing_detail.html template.
    repo = ListingRepository(db)
    listing = await repo.get_by_id(listing_id)
    if not listing:
        return HTMLResponse("<h1>Not found</h1>", status_code=404)

    return templates.TemplateResponse(
        "listing_detail.html",
        {
            "request": request,
            "active": "listings",
            "listing": listing,
        },
    )


@router.get("/runs", response_class=HTMLResponse)
async def admin_runs(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    # Total count for pagination header
    total = await db.scalar(select(func.count()).select_from(ScrapeRun)) or 0

    offset = (page - 1) * page_size
    result = await db.execute(
        select(ScrapeRun)
        .order_by(ScrapeRun.started_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    runs = result.scalars().all()
    pages = math.ceil(total / page_size) if page_size else 1

    # Per-run error counts
    run_ids = [r.id for r in runs]
    error_counts: dict = {}
    if run_ids:
        rows = await db.execute(
            select(ScraperError.run_id, func.count().label("cnt"))
            .where(ScraperError.run_id.in_(run_ids))
            .group_by(ScraperError.run_id)
        )
        error_counts = {row.run_id: row.cnt for row in rows}

    return templates.TemplateResponse(
        "runs.html",
        {
            "request": request,
            "active": "runs",
            "runs": runs,
            "error_counts": error_counts,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
        },
    )


@router.get("/errors", response_class=HTMLResponse)
async def admin_errors(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ScraperError).order_by(ScraperError.created_at.desc()).limit(100)
    )
    errors = result.scalars().all()
    return templates.TemplateResponse(
        "errors.html",
        {"request": request, "active": "errors", "errors": errors},
    )


@router.get("/stats", response_class=HTMLResponse)
async def admin_stats(request: Request, db: AsyncSession = Depends(get_db)):
    repo = ListingRepository(db)
    url_repo = ListingURLRepository(db)

    stats_data = await repo.get_statistics()
    url_counts = await url_repo.get_counts()

    combined = {
        **stats_data,
        "pending_urls": url_counts.get("pending", 0),
        "failed_urls": url_counts.get("failed", 0),
        "scraped_urls": url_counts.get("done", 0),
    }

    return templates.TemplateResponse(
        "stats.html",
        {"request": request, "active": "stats", "stats": combined},
    )


@router.get("/proxies", response_class=HTMLResponse)
async def admin_proxies(request: Request, db: AsyncSession = Depends(get_db)):
    """Proxy pool health + throttling/rate-limit panel + failed-URL
    auto-requeue status. Proxy stats are pulled from Redis (published by the
    live worker processes), not this process's own idle ProxyManager --
    see get_live_proxy_stats() in app.scraper.proxy_manager. Throttle stats + requeue counts are
    DB/Redis-backed already."""
    url_repo = ListingURLRepository(db)

    proxy_stats = await get_live_proxy_stats()
    throttle_stats = await domain_throttler.stats()
    requeue_eta = await url_repo.get_failed_requeue_eta()

    healthy_count = sum(1 for e in proxy_stats["endpoints"] if not e["in_cooldown"])

    return templates.TemplateResponse(
        "proxies.html",
        {
            "request": request,
            "active": "proxies",
            "proxy_stats": proxy_stats,
            "healthy_count": healthy_count,
            "cooldown_count": proxy_stats["pool_size"] - healthy_count,
            "throttle_stats": throttle_stats,
            "requeue_eta": requeue_eta,
            "config": {
                "proxy_provider": settings.proxy_provider,
                "proxy_max_concurrent_per_session": settings.proxy_max_concurrent_per_session,
                "proxy_global_rps_limit": settings.proxy_global_rps_limit,
                "proxy_failure_threshold": settings.proxy_failure_threshold,
                "proxy_cooldown_seconds": settings.proxy_cooldown_seconds,
                "proxy_max_retries_per_request": settings.proxy_max_retries_per_request,
                "proxy_retry_backoff_base_seconds": settings.proxy_retry_backoff_base_seconds,
                "proxy_rotate_every_n_requests": settings.proxy_rotate_every_n_requests,
                "pincali_max_retries_per_url": settings.pincali_max_retries_per_url,
                "pincali_failed_url_requeue_after_hours": settings.pincali_failed_url_requeue_after_hours,
            },
        },
    )