"""
app/api/v1/listings.py — REST API endpoints for listings.
"""
from __future__ import annotations

import math
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.repositories.listings import ListingRepository
from app.schemas.listings import (
    ListingFilters,
    ListingResponse,
    PaginatedListings,
    SearchResponse,
    StatisticsResponse,
    GeographyCatalogResponse,
    GeoPaginatedStates,
    GeoPaginatedCities,
    GeoPaginatedCommunities,
    GeoPaginatedSubdivisions,
    RawAddressResponse,
    PaginatedRawAddresses,
)

router = APIRouter(prefix="/listings", tags=["listings"])


@router.get("", response_model=PaginatedListings)
async def list_listings(
    city: Optional[str] = Query(None, description="Filter by city (partial match)"),
    state: Optional[str] = Query(None, description="Filter by state (partial match)"),
    community: Optional[str] = Query(None, description="Filter by community (partial match)"),
    subdivision: Optional[str] = Query(None, description="Filter by subdivision (partial match)"),
    min_price: Optional[float] = Query(None, ge=0),
    max_price: Optional[float] = Query(None, ge=0),
    beds: Optional[int] = Query(None, ge=0),
    baths: Optional[int] = Query(None, ge=0),
    property_type: Optional[str] = Query(None),
    mls_status: Optional[str] = Query(None),
    sort_by: str = Query("created_at", pattern="^(created_at|updated_at|price_usd|beds_total)$"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """
    List all listings with optional filters, sorting, and pagination.
    """
    filters = ListingFilters(
        city=city,
        state=state,
        community=community,
        subdivision=subdivision,
        min_price=min_price,
        max_price=max_price,
        beds=beds,
        baths=baths,
        property_type=property_type,
        mls_status=mls_status,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        page_size=page_size,
    )
    repo = ListingRepository(db)
    items, total = await repo.list_paginated(filters)

    # Build response with primary photo
    result_items = []
    for listing in items:
        primary_photo = None
        if listing.photos:
            primary = next((p for p in listing.photos if p.is_primary), None)
            if primary:
                primary_photo = primary.url
            elif listing.photos:
                primary_photo = listing.photos[0].url

        result_items.append(
            {
                "id": listing.id,
                "external_id": listing.external_id,
                "source": listing.source,
                "origin_url": listing.origin_url,
                "name": listing.name,
                "price_usd": listing.price_usd,
                "mls_status": listing.mls_status,
                "property_type": listing.property_type,
                "city": listing.city,
                "beds_total": listing.beds_total,
                "baths_full": listing.baths_full,
                "lot_size_m2": listing.lot_size_m2,
                "construction_m2": listing.construction_m2,
                "latitude": listing.latitude,
                "longitude": listing.longitude,
                "primary_photo": primary_photo,
                "created_at": listing.created_at,
                "updated_at": listing.updated_at,
            }
        )

    pages = math.ceil(total / page_size) if page_size else 1

    return PaginatedListings(
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
        items=result_items,
    )


@router.get("/search", response_model=SearchResponse)
async def search_listings(
    q: str = Query(..., min_length=2, description="Search query"),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Full-text search across name, description, city, community."""
    repo = ListingRepository(db)
    items = await repo.search(q, limit=limit)

    result_items = []
    for listing in items:
        primary_photo = None
        if listing.photos:
            primary = next((p for p in listing.photos if p.is_primary), None)
            primary_photo = (primary or listing.photos[0]).url if (primary or listing.photos) else None

        result_items.append(
            {
                "id": listing.id,
                "external_id": listing.external_id,
                "source": listing.source,
                "origin_url": listing.origin_url,
                "name": listing.name,
                "price_usd": listing.price_usd,
                "mls_status": listing.mls_status,
                "property_type": listing.property_type,
                "city": listing.city,
                "beds_total": listing.beds_total,
                "baths_full": listing.baths_full,
                "lot_size_m2": listing.lot_size_m2,
                "construction_m2": listing.construction_m2,
                "latitude": listing.latitude,
                "longitude": listing.longitude,
                "primary_photo": primary_photo,
                "created_at": listing.created_at,
                "updated_at": listing.updated_at,
            }
        )

    return SearchResponse(query=q, total=len(result_items), items=result_items)


@router.get("/statistics", response_model=StatisticsResponse)
async def get_statistics(db: AsyncSession = Depends(get_db)):
    """Return platform-wide statistics."""
    from app.repositories.listing_urls import ListingURLRepository
    from sqlalchemy import select, func
    from app.models.orm import ScrapeRun

    repo = ListingRepository(db)
    url_repo = ListingURLRepository(db)

    stats = await repo.get_statistics()
    url_counts = await url_repo.get_counts()

    # Last run
    result = await db.execute(
        select(ScrapeRun)
        .where(ScrapeRun.run_type == "detail")
        .order_by(ScrapeRun.started_at.desc())
        .limit(1)
    )
    last_run = result.scalar_one_or_none()

    return StatisticsResponse(
        total_listings=stats["total_listings"],
        active_listings=stats["active_listings"],
        pending_urls=url_counts.get("pending", 0),
        failed_urls=url_counts.get("failed", 0),
        scraped_urls=url_counts.get("done", 0),
        listings_by_status=stats["listings_by_status"],
        listings_by_type=stats["listings_by_type"],
        listings_by_city=stats["listings_by_city"],
        avg_price_usd=stats["avg_price_usd"],
        last_run_at=last_run.started_at if last_run else None,
    )


@router.get("/geography", response_model=GeographyCatalogResponse)
async def get_geography_catalog(
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(
        100,
        ge=1,
        le=500,
        description="Number of items per page for each geographic level",
    ),
    state: Optional[str] = Query(
        None,
        description="Filter cities, communities, and subdivisions by state (partial match)",
    ),
    city: Optional[str] = Query(
        None,
        description="Filter communities and subdivisions by city (partial match)",
    ),
    community: Optional[str] = Query(
        None,
        description="Filter subdivisions by community (partial match)",
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Return the paginated geographic hierarchy catalog.

    Each level (states, cities, communities, subdivisions) is returned
    as a paginated object with `items`, `total`, `page`, `page_size`,
    and `pages` fields.

    **Pagination** — `page` and `page_size` apply to every level.
    Increase `page` to walk through large result sets.

    **Hierarchy filters** — narrow one or more levels at once:
    - `state` filters cities, communities, and subdivisions
    - `city` filters communities and subdivisions
    - `community` filters subdivisions only

    Typical usage for populating filter dropdowns:
    1. First call with default params to get all states + first page of cities.
    2. When the user picks a state, re-call with `?state=Quintana+Roo` to
       get cities/communities/subdivisions scoped to that state.
    """
    repo = ListingRepository(db)
    return await repo.get_geography_catalog(
        page=page,
        page_size=page_size,
        state=state,
        city=city,
        community=community,
    )


# ── Focused single-level geography endpoints ─────────────────────────────
# These replace the combined /geography endpoint for client-facing use.
# Each endpoint runs exactly ONE query so searching for, e.g., a community
# never returns an unrelated dump of all states and cities.
# All four must be declared BEFORE /{listing_id} to avoid being swallowed
# by the int-cast catch-all.

@router.get(
    "/geography/states",
    response_model=GeoPaginatedStates,
    summary="List all distinct states",
    tags=["geography"],
)
async def get_geo_states(
    state: Optional[str] = Query(
        None,
        description="Search within state names (partial, case-insensitive)",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """
    Return distinct states that have at least one listing.

    Use `?state=Baja` to filter states whose name contains "Baja".
    """
    repo = ListingRepository(db)
    return await repo.get_geo_states(page=page, page_size=page_size, state=state)


@router.get(
    "/geography/cities",
    response_model=GeoPaginatedCities,
    summary="List all distinct cities",
    tags=["geography"],
)
async def get_geo_cities(
    state: Optional[str] = Query(
        None,
        description="Restrict to a specific state (partial, case-insensitive)",
    ),
    city: Optional[str] = Query(
        None,
        description="Search within city names (partial, case-insensitive)",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """
    Return distinct (state, city) pairs.

    Narrow to a state first to avoid loading every city in Mexico:
    `?state=Baja+California`
    """
    repo = ListingRepository(db)
    return await repo.get_geo_cities(
        page=page, page_size=page_size, state=state, city=city
    )


@router.get(
    "/geography/communities",
    response_model=GeoPaginatedCommunities,
    summary="List all distinct communities",
    tags=["geography"],
)
async def get_geo_communities(
    state: Optional[str] = Query(
        None,
        description="Filter by state (partial, case-insensitive)",
    ),
    city: Optional[str] = Query(
        None,
        description="Filter by city (partial, case-insensitive)",
    ),
    community: Optional[str] = Query(
        None,
        description="Search within community names (partial, case-insensitive)",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """
    Return distinct (state, city, community) triples.

    Example — find "Valle de Guadalupe" without the full-hierarchy dump:
    `?community=Valle+de+Guadalupe`
    """
    repo = ListingRepository(db)
    return await repo.get_geo_communities(
        page=page,
        page_size=page_size,
        state=state,
        city=city,
        community=community,
    )


@router.get(
    "/geography/subdivisions",
    response_model=GeoPaginatedSubdivisions,
    summary="List all distinct subdivisions",
    tags=["geography"],
)
async def get_geo_subdivisions(
    state: Optional[str] = Query(
        None,
        description="Filter by state (partial, case-insensitive)",
    ),
    city: Optional[str] = Query(
        None,
        description="Filter by city (partial, case-insensitive)",
    ),
    community: Optional[str] = Query(
        None,
        description="Filter by community (partial, case-insensitive)",
    ),
    subdivision: Optional[str] = Query(
        None,
        description="Search within subdivision names (partial, case-insensitive)",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """
    Return distinct (state, city, community, subdivision) tuples.

    Scope progressively:
    `?state=Baja+California&city=Ensenada&community=Valle+de+Guadalupe`
    """
    repo = ListingRepository(db)
    return await repo.get_geo_subdivisions(
        page=page,
        page_size=page_size,
        state=state,
        city=city,
        community=community,
        subdivision=subdivision,
    )


@router.get(
    "/raw-addresses",
    response_model=PaginatedRawAddresses,
    summary="List raw address strings with parsed geo fields",
    description="""
    Returns the original, unparsed location strings alongside the parsed
    geographic fields for every listing, so you can verify the parser
    extracted the right values.

    **Filters**
    - `state` — restrict to listings in a particular state (partial match)
    - `city`  — restrict to a particular city (partial match)
    - `missing_only` — return only listings where `raw_address` was not captured

    All results are paginated via `page` / `page_size`.
    """,
)
async def list_raw_addresses(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    state: Optional[str] = Query(None, description="Filter by state (partial match)"),
    city: Optional[str] = Query(None, description="Filter by city (partial match)"),
    missing_only: bool = Query(
        False,
        description="Return only listings where raw_address was not captured",
    ),
    db: AsyncSession = Depends(get_db),
):
    repo = ListingRepository(db)
    items, total = await repo.list_raw_addresses(
        page=page,
        page_size=page_size,
        state=state,
        city=city,
        missing_only=missing_only,
    )
    pages = math.ceil(total / page_size) if page_size else 1
    return PaginatedRawAddresses(
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
        items=items,
    )


# NOTE: /{listing_id} is a catch-all int route — keep it AFTER all static
# sub-paths (/search, /statistics, /geography, /raw-addresses) so FastAPI
# matches those first.
@router.get("/{listing_id}", response_model=ListingResponse)
async def get_listing(listing_id: int, db: AsyncSession = Depends(get_db)):
    """Get a single listing with all details."""
    repo = ListingRepository(db)
    listing = await repo.get_by_id(listing_id)
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
    return listing


@router.get(
    "/{listing_id}/raw-address",
    response_model=RawAddressResponse,
    summary="Raw address strings for a single listing",
    description="""
    Returns the unprocessed location strings alongside the parsed
    geographic fields for one listing.  Use this to spot-check parser
    output for a specific property.

    **Response fields**
    - `raw_address` — the full comma-separated string from the listing's
      map/location section (right-to-left hierarchy: last segment = state)
    - `location_subtitle_raw` — the page subtitle (e.g. "Casa en Venta en Pachuca de Soto, Hidalgo")
    - `parsed` — the city/state/community/subdivision values stored in the DB
    """,
)
async def get_raw_address(listing_id: int, db: AsyncSession = Depends(get_db)):
    repo = ListingRepository(db)
    data = await repo.get_raw_address(listing_id)
    if not data:
        raise HTTPException(status_code=404, detail="Listing not found")
    return data