"""
app/repositories/listings.py — Database access layer for listings.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, select, update, delete, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.orm import (
    Listing,
    ListingChange,
    ListingPhoto,
    ListingVideo,
    VirtualTour,
)
from app.schemas.listings import ListingFilters


class ListingRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, listing_id: int) -> Optional[Listing]:
        result = await self.session.execute(
            select(Listing)
            .options(
                selectinload(Listing.photos),
                selectinload(Listing.videos),
                selectinload(Listing.virtual_tours),
            )
            .where(Listing.id == listing_id)
        )
        return result.scalar_one_or_none()

    async def get_by_external_id(
        self, external_id: str, source: str = "pincali"
    ) -> Optional[Listing]:
        result = await self.session.execute(
            select(Listing).where(
                and_(
                    Listing.external_id == external_id,
                    Listing.source == source,
                )
            )
        )
        return result.scalar_one_or_none()

    async def list_paginated(
        self, filters: ListingFilters
    ) -> Tuple[List[Listing], int]:
        query = select(Listing)
        count_query = select(func.count()).select_from(Listing)

        # Apply filters
        conditions = []
        if filters.city:
            conditions.append(Listing.city.ilike(f"%{filters.city}%"))
        if filters.state:
            conditions.append(Listing.state.ilike(f"%{filters.state}%"))
        if filters.community:
            conditions.append(Listing.community.ilike(f"%{filters.community}%"))
        if filters.subdivision:
            conditions.append(Listing.subdivision.ilike(f"%{filters.subdivision}%"))
        if filters.min_price is not None:
            conditions.append(Listing.price_usd >= filters.min_price)
        if filters.max_price is not None:
            conditions.append(Listing.price_usd <= filters.max_price)
        if filters.beds is not None:
            conditions.append(Listing.beds_total >= filters.beds)
        if filters.baths is not None:
            conditions.append(Listing.baths_full >= filters.baths)
        if filters.property_type:
            conditions.append(Listing.property_type == filters.property_type)
        if filters.mls_status:
            conditions.append(Listing.mls_status == filters.mls_status)

        if conditions:
            query = query.where(and_(*conditions))
            count_query = count_query.where(and_(*conditions))

        # Sort
        sort_col = getattr(Listing, filters.sort_by, Listing.created_at)
        if filters.sort_dir == "asc":
            query = query.order_by(sort_col.asc())
        else:
            query = query.order_by(sort_col.desc())

        # Count
        total_result = await self.session.execute(count_query)
        total = total_result.scalar_one()

        # Paginate
        offset = (filters.page - 1) * filters.page_size
        query = query.offset(offset).limit(filters.page_size)

        # Load with photos for primary image
        query = query.options(selectinload(Listing.photos))
        result = await self.session.execute(query)
        items = result.scalars().all()

        return list(items), total

    async def search(self, q: str, limit: int = 20) -> List[Listing]:
        q_lower = f"%{q.lower()}%"
        result = await self.session.execute(
            select(Listing)
            .where(
                or_(
                    Listing.name.ilike(q_lower),
                    Listing.description.ilike(q_lower),
                    Listing.city.ilike(q_lower),
                    Listing.community.ilike(q_lower),
                )
            )
            .options(selectinload(Listing.photos))
            .limit(limit)
        )
        return list(result.scalars().all())

    async def upsert(
        self, data: Dict, exchange_rate: float = 20.5
    ) -> Tuple[Listing, bool, List[str]]:
        """
        Insert or update a listing.
        Returns (listing, created, changed_fields).
        """
        external_id = data.get("external_id", "")
        source = data.get("source", "pincali")

        existing = await self.get_by_external_id(external_id, source)
        changed_fields: List[str] = []

        # Compute USD price from original_price (set by normalizer)
        raw_price = data.get("original_price") or data.get("price_usd") or data.get("price")
        orig_currency = (data.get("original_currency") or "MXN").strip().upper()
        price_usd = None
        if raw_price is not None:
            try:
                raw_price = float(raw_price)
                if orig_currency == "MXN":
                    price_usd = raw_price / exchange_rate
                else:
                    price_usd = raw_price
            except (ValueError, TypeError):
                pass

        listing_data = {
            "external_id": external_id,
            "source": source,
            "origin_url": data.get("origin_url", ""),
            "name": data.get("name"),
            "description": data.get("description"),
            "supplement": data.get("supplement"),
            "price_usd": price_usd,
            "original_price": raw_price,
            "original_currency": orig_currency,
            "mls_status": data.get("mls_status"),
            "property_type": data.get("property_type"),
            "city": data.get("city"),
            "state": data.get("state"),
            "country": data.get("country", "Mexico"),
            "community": data.get("community"),
            "subdivision": data.get("subdivision"),
            "latitude": data.get("latitude"),
            "longitude": data.get("longitude"),
            "beds_total": data.get("beds_total"),
            "baths_full": data.get("baths_full"),
            "baths_half": data.get("baths_half"),
            "year_built": data.get("year_built"),
            "lot_size_m2": data.get("lot_size_m2"),
            "construction_m2": data.get("construction_m2"),
            "living_area_m2": data.get("living_area_m2"),
            "stories": data.get("stories"),
            "parking_spaces": data.get("parking_spaces"),
            "pool": data.get("pool"),
            "furnished": data.get("furnished"),
            "view": data.get("view"),
            "interior_features": data.get("interior_features"),
            "exterior_features": data.get("exterior_features"),
            "list_agent_name": data.get("list_agent_name"),
            "list_office_name": data.get("list_office_name"),
            "extra": data.get("extra"),
            "last_seen_at": datetime.utcnow(),
        }

        if existing is None:
            listing = Listing(**listing_data)
            self.session.add(listing)
            await self.session.flush()
            created = True
        else:
            # Track changed fields for auditing
            track_fields = [
                "price_usd", "mls_status", "city", "beds_total",
                "baths_full", "lot_size_m2", "construction_m2",
            ]
            for field in track_fields:
                old_val = getattr(existing, field)
                new_val = listing_data.get(field)
                # BUG FIX #3: track changes even when new_val is None (field cleared)
                if old_val != new_val:
                    changed_fields.append(field)
                    change = ListingChange(
                        listing_id=existing.id,
                        field_name=field,
                        old_value=str(old_val) if old_val is not None else None,
                        new_value=str(new_val) if new_val is not None else None,
                    )
                    self.session.add(change)

            # BUG FIX #3: always write every field, including None values,
            # so stale data is cleared when the source no longer provides it.
            for key, val in listing_data.items():
                setattr(existing, key, val)
            listing = existing
            created = False

        return listing, created, changed_fields

    async def bulk_upsert_media(
        self,
        listing_id: int,
        photos: List[Dict],
        videos: List[Dict],
        tours: List[Dict],
    ) -> None:
        """Replace all media for a listing."""
        # Delete existing
        await self.session.execute(
            delete(ListingPhoto).where(ListingPhoto.listing_id == listing_id)
        )
        await self.session.execute(
            delete(ListingVideo).where(ListingVideo.listing_id == listing_id)
        )
        await self.session.execute(
            delete(VirtualTour).where(VirtualTour.listing_id == listing_id)
        )

        for photo in photos:
            self.session.add(ListingPhoto(listing_id=listing_id, **photo))
        for video in videos:
            self.session.add(ListingVideo(listing_id=listing_id, **video))
        for tour in tours:
            self.session.add(VirtualTour(listing_id=listing_id, **tour))

    async def get_statistics(self) -> Dict:
        total = await self.session.scalar(select(func.count()).select_from(Listing))

        active = await self.session.scalar(
            select(func.count())
            .select_from(Listing)
            .where(Listing.mls_status == "Active")
        )

        avg_price = await self.session.scalar(
            select(func.avg(Listing.price_usd)).where(Listing.price_usd.isnot(None))
        )

        # By status
        status_rows = await self.session.execute(
            select(Listing.mls_status, func.count())
            .group_by(Listing.mls_status)
        )
        by_status = {row[0] or "Unknown": row[1] for row in status_rows}

        # By type
        type_rows = await self.session.execute(
            select(Listing.property_type, func.count())
            .group_by(Listing.property_type)
        )
        by_type = {row[0] or "Unknown": row[1] for row in type_rows}

        # By city (top 10)
        city_rows = await self.session.execute(
            select(Listing.city, func.count())
            .where(Listing.city.isnot(None))
            .group_by(Listing.city)
            .order_by(func.count().desc())
            .limit(10)
        )
        by_city = {row[0]: row[1] for row in city_rows}

        return {
            "total_listings": total or 0,
            "active_listings": active or 0,
            "avg_price_usd": round(avg_price, 2) if avg_price else None,
            "listings_by_status": by_status,
            "listings_by_type": by_type,
            "listings_by_city": by_city,
        }

    async def get_geography_catalog(
        self,
        page: int = 1,
        page_size: int = 100,
        state: Optional[str] = None,
        city: Optional[str] = None,
        community: Optional[str] = None,
    ) -> Dict:
        """
        Return distinct geographic values grouped hierarchically, with
        pagination and optional hierarchy filters.

        ``page`` / ``page_size`` apply to every level independently so
        each level's ``items`` slice starts at the same logical offset.
        Use the hierarchy filters to drill into a branch without fetching
        the entire catalog:
          - ``state``     → narrows cities, communities, subdivisions
          - ``city``      → narrows communities, subdivisions
          - ``community`` → narrows subdivisions only
        """
        offset = (page - 1) * page_size

        def _pages(total: int) -> int:
            return math.ceil(total / page_size) if page_size else 1

        # ── STATES ───────────────────────────────────────────────────────────
        # When a state filter is supplied, scope states down to matching entries
        # so the response only contains the requested state(s).  When no filter
        # is provided every distinct state is returned (useful for dropdown init).
        state_base = (
            select(Listing.state)
            .where(Listing.state.isnot(None))
            .distinct()
        )
        if state:
            state_base = state_base.where(Listing.state.ilike(f"%{state}%"))
        state_total = await self.session.scalar(
            select(func.count()).select_from(state_base.subquery())
        )
        state_rows = await self.session.execute(
            state_base.order_by(Listing.state).offset(offset).limit(page_size)
        )
        states = [{"state": r[0]} for r in state_rows]

        # ── CITIES (filter by state) ─────────────────────────────────────
        city_base = (
            select(Listing.state, Listing.city)
            .where(Listing.city.isnot(None))
        )
        if state:
            city_base = city_base.where(Listing.state.ilike(f"%{state}%"))
        city_base = city_base.distinct()

        city_total = await self.session.scalar(
            select(func.count()).select_from(city_base.subquery())
        )
        city_rows = await self.session.execute(
            city_base
            .order_by(Listing.state, Listing.city)
            .offset(offset)
            .limit(page_size)
        )
        cities = [{"state": r[0], "city": r[1]} for r in city_rows]

        # ── COMMUNITIES (filter by state + city) ─────────────────────────
        comm_base = (
            select(Listing.state, Listing.city, Listing.community)
            .where(Listing.community.isnot(None))
        )
        if state:
            comm_base = comm_base.where(Listing.state.ilike(f"%{state}%"))
        if city:
            comm_base = comm_base.where(Listing.city.ilike(f"%{city}%"))
        comm_base = comm_base.distinct()

        comm_total = await self.session.scalar(
            select(func.count()).select_from(comm_base.subquery())
        )
        comm_rows = await self.session.execute(
            comm_base
            .order_by(Listing.state, Listing.city, Listing.community)
            .offset(offset)
            .limit(page_size)
        )
        communities = [
            {"state": r[0], "city": r[1], "community": r[2]}
            for r in comm_rows
        ]

        # ── SUBDIVISIONS (filter by state + city + community) ─────────────
        sub_base = (
            select(
                Listing.state,
                Listing.city,
                Listing.community,
                Listing.subdivision,
            )
            .where(Listing.subdivision.isnot(None))
        )
        if state:
            sub_base = sub_base.where(Listing.state.ilike(f"%{state}%"))
        if city:
            sub_base = sub_base.where(Listing.city.ilike(f"%{city}%"))
        if community:
            sub_base = sub_base.where(Listing.community.ilike(f"%{community}%"))
        sub_base = sub_base.distinct()

        sub_total = await self.session.scalar(
            select(func.count()).select_from(sub_base.subquery())
        )
        sub_rows = await self.session.execute(
            sub_base
            .order_by(
                Listing.state,
                Listing.city,
                Listing.community,
                Listing.subdivision,
            )
            .offset(offset)
            .limit(page_size)
        )
        subdivisions = [
            {"state": r[0], "city": r[1], "community": r[2], "subdivision": r[3]}
            for r in sub_rows
        ]

        return {
            "states": {
                "items": states,
                "total": state_total or 0,
                "page": page,
                "page_size": page_size,
                "pages": _pages(state_total or 0),
            },
            "cities": {
                "items": cities,
                "total": city_total or 0,
                "page": page,
                "page_size": page_size,
                "pages": _pages(city_total or 0),
            },
            "communities": {
                "items": communities,
                "total": comm_total or 0,
                "page": page,
                "page_size": page_size,
                "pages": _pages(comm_total or 0),
            },
            "subdivisions": {
                "items": subdivisions,
                "total": sub_total or 0,
                "page": page,
                "page_size": page_size,
                "pages": _pages(sub_total or 0),
            },
        }

    # ── Individual geography catalogs ────────────────────────────────────────
    # Each method runs only ONE distinct query (+ one COUNT).  Use these from
    # the /geography/states|cities|communities|subdivisions endpoints so a
    # search for, say, "Valle de Guadalupe" returns only communities — not
    # the full four-level blob that /geography returns.

    async def get_geo_states(
        self,
        page: int = 1,
        page_size: int = 100,
        state: Optional[str] = None,
    ) -> Dict:
        """Distinct states that have at least one listing."""
        offset = (page - 1) * page_size
        base = select(Listing.state).where(Listing.state.isnot(None))
        if state:
            base = base.where(Listing.state.ilike(f"%{state}%"))
        base = base.distinct()
        total = await self.session.scalar(
            select(func.count()).select_from(base.subquery())
        ) or 0
        rows = await self.session.execute(
            base.order_by(Listing.state).offset(offset).limit(page_size)
        )
        pages = math.ceil(total / page_size) if page_size else 1
        return {
            "items": [{"state": r[0]} for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
        }

    async def get_geo_cities(
        self,
        page: int = 1,
        page_size: int = 100,
        state: Optional[str] = None,
        city: Optional[str] = None,
    ) -> Dict:
        """Distinct (state, city) pairs, optionally scoped to a state."""
        offset = (page - 1) * page_size
        base = select(Listing.state, Listing.city).where(Listing.city.isnot(None))
        if state:
            base = base.where(Listing.state.ilike(f"%{state}%"))
        if city:
            base = base.where(Listing.city.ilike(f"%{city}%"))
        base = base.distinct()
        total = await self.session.scalar(
            select(func.count()).select_from(base.subquery())
        ) or 0
        rows = await self.session.execute(
            base.order_by(Listing.state, Listing.city).offset(offset).limit(page_size)
        )
        pages = math.ceil(total / page_size) if page_size else 1
        return {
            "items": [{"state": r[0], "city": r[1]} for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
        }

    async def get_geo_communities(
        self,
        page: int = 1,
        page_size: int = 100,
        state: Optional[str] = None,
        city: Optional[str] = None,
        community: Optional[str] = None,
    ) -> Dict:
        """Distinct (state, city, community) triples, optionally scoped."""
        offset = (page - 1) * page_size
        base = (
            select(Listing.state, Listing.city, Listing.community)
            .where(Listing.community.isnot(None))
        )
        if state:
            base = base.where(Listing.state.ilike(f"%{state}%"))
        if city:
            base = base.where(Listing.city.ilike(f"%{city}%"))
        if community:
            base = base.where(Listing.community.ilike(f"%{community}%"))
        base = base.distinct()
        total = await self.session.scalar(
            select(func.count()).select_from(base.subquery())
        ) or 0
        rows = await self.session.execute(
            base.order_by(Listing.state, Listing.city, Listing.community)
            .offset(offset)
            .limit(page_size)
        )
        pages = math.ceil(total / page_size) if page_size else 1
        return {
            "items": [
                {"state": r[0], "city": r[1], "community": r[2]} for r in rows
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
        }

    async def get_geo_subdivisions(
        self,
        page: int = 1,
        page_size: int = 100,
        state: Optional[str] = None,
        city: Optional[str] = None,
        community: Optional[str] = None,
        subdivision: Optional[str] = None,
    ) -> Dict:
        """Distinct (state, city, community, subdivision) tuples, optionally scoped."""
        offset = (page - 1) * page_size
        base = (
            select(
                Listing.state,
                Listing.city,
                Listing.community,
                Listing.subdivision,
            )
            .where(Listing.subdivision.isnot(None))
        )
        if state:
            base = base.where(Listing.state.ilike(f"%{state}%"))
        if city:
            base = base.where(Listing.city.ilike(f"%{city}%"))
        if community:
            base = base.where(Listing.community.ilike(f"%{community}%"))
        if subdivision:
            base = base.where(Listing.subdivision.ilike(f"%{subdivision}%"))
        base = base.distinct()
        total = await self.session.scalar(
            select(func.count()).select_from(base.subquery())
        ) or 0
        rows = await self.session.execute(
            base.order_by(
                Listing.state,
                Listing.city,
                Listing.community,
                Listing.subdivision,
            )
            .offset(offset)
            .limit(page_size)
        )
        pages = math.ceil(total / page_size) if page_size else 1
        return {
            "items": [
                {
                    "state": r[0],
                    "city": r[1],
                    "community": r[2],
                    "subdivision": r[3],
                }
                for r in rows
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
        }

    async def get_raw_address(self, listing_id: int) -> Optional[Dict]:
        """
        Return the raw location strings alongside the parsed geo fields
        for a single listing.  Used by the /raw-address endpoint.
        """
        result = await self.session.execute(
            select(Listing).where(Listing.id == listing_id)
        )
        listing = result.scalar_one_or_none()
        if listing is None:
            return None

        extra = listing.extra or {}
        return {
            "listing_id": listing.id,
            "external_id": listing.external_id,
            "origin_url": listing.origin_url,
            "raw_address": extra.get("raw_address"),
            "location_subtitle_raw": extra.get("location_subtitle_raw"),
            "parsed": {
                "subdivision": listing.subdivision,
                "community": listing.community,
                "city": listing.city,
                "state": listing.state,
                "country": listing.country,
                "latitude": listing.latitude,
                "longitude": listing.longitude,
            },
        }

    async def list_raw_addresses(
        self,
        page: int = 1,
        page_size: int = 50,
        state: Optional[str] = None,
        city: Optional[str] = None,
        missing_only: bool = False,
    ) -> Tuple[List[Dict], int]:
        """
        Return raw address + parsed geo data for multiple listings,
        with optional filters.  ``missing_only=True`` restricts to
        listings where the raw_address was not captured (useful for
        identifying records that need a re-scrape).
        """
        query = select(Listing)
        count_query = select(func.count()).select_from(Listing)

        conditions = []
        if state:
            conditions.append(Listing.state.ilike(f"%{state}%"))
        if city:
            conditions.append(Listing.city.ilike(f"%{city}%"))
        if conditions:
            query = query.where(and_(*conditions))
            count_query = count_query.where(and_(*conditions))

        total = await self.session.scalar(count_query) or 0

        offset = (page - 1) * page_size
        query = query.order_by(Listing.id.asc()).offset(offset).limit(page_size)
        result = await self.session.execute(query)
        listings = result.scalars().all()

        rows = []
        for l in listings:
            extra = l.extra or {}
            raw_addr = extra.get("raw_address")
            if missing_only and raw_addr:
                continue
            rows.append({
                "listing_id": l.id,
                "external_id": l.external_id,
                "origin_url": l.origin_url,
                "raw_address": raw_addr,
                "location_subtitle_raw": extra.get("location_subtitle_raw"),
                "parsed": {
                    "subdivision": l.subdivision,
                    "community": l.community,
                    "city": l.city,
                    "state": l.state,
                    "country": l.country,
                    "latitude": l.latitude,
                    "longitude": l.longitude,
                },
            })

        return rows, total