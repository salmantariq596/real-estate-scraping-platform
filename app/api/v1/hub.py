"""
Hub Listings API Endpoint
=========================
GET /api/v1/hub/listings
Returns listings in the exact client integration format.
All fields always present — null / [] / {} when unavailable.
"""

from datetime import datetime, timezone
from typing import List, Optional, Any
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.models import Listing, ListingPhoto, ListingVideo, VirtualTour

router = APIRouter(prefix="/hub", tags=["hub"])


# ──────────────────────────────────────────────────────────────
# Pydantic Response Schemas (exact client format)
# ──────────────────────────────────────────────────────────────

class HubPhoto(BaseModel):
    """Photo object in the exact client format."""
    url: str = Field(..., description="Direct URL to the image. Must be publicly accessible.")
    position: int = Field(..., ge=0, description="Display order starting at 0")
    is_primary: Optional[bool] = Field(None, description="Whether this is the main/cover photo. Defaults to position === 0")
    caption: Optional[str] = Field(None, description="Photo description")

    class Config:
        from_attributes = True


class HubVideo(BaseModel):
    """Video object in the exact client format."""
    url: str = Field(..., description="Video URL. Supported: YouTube, Vimeo, SproutVideo")
    title: Optional[str] = Field(None, description="Video title")

    class Config:
        from_attributes = True


class HubVirtualTour(BaseModel):
    """Virtual tour object in the exact client format."""
    url: str = Field(..., description="Tour URL. Supported: Matterport, Ricoh360")
    title: Optional[str] = Field(None, description="Tour title")

    class Config:
        from_attributes = True


class HubListing(BaseModel):
    """
    Single listing in the exact format the client expects.
    Every field below is ALWAYS serialized even if the underlying DB value is None.
    """
    external_id: str = Field(..., description="Unique ID of the listing in the source portal. Used for deduplication.")
    origin_url: str = Field(..., description="Full URL of the original listing page")
    name: str = Field(..., description="Listing title. Lowercase (hub controls display capitalization via CSS)")
    description: Optional[str] = Field(None, description="Full property description")
    supplement: Optional[str] = Field(None, description="Additional info (HOA rules, amenities, restrictions)")

    price: float = Field(..., gt=0, description="Listing price in USD. No formatting, no currency symbols.")
    mls_status: str = Field(..., description="One of: Active, Pending, Sold, Cancelled")
    property_type: str = Field(..., description="Single character code: A=Condo, B=House, D=Reservations, E=Land, F=Commercial, H=Fractional, I=Multi-Family")

    city: Optional[str] = Field(None, description="City or zone name. Lowercase")
    mls_area_major: Optional[str] = Field(None, description="General area within the city")
    community: Optional[str] = Field(None, description="Community or neighborhood name")
    subdivision: Optional[str] = Field(None, description="Subdivision within the community")
    latitude: Optional[float] = Field(None, ge=-90, le=90, description="Decimal degrees. Required for map display.")
    longitude: Optional[float] = Field(None, ge=-180, le=180, description="Decimal degrees. Required for map display.")

    beds_total: Optional[int] = Field(None, description="Total bedrooms")
    baths_full: Optional[int] = Field(None, description="Full bathrooms")
    baths_half: Optional[int] = Field(None, description="Half bathrooms")
    year_built: Optional[int] = Field(None, description="Year of construction")
    lot_size_m2: Optional[float] = Field(None, description="Lot/land size in square meters")
    construction_m2: Optional[float] = Field(None, description="Total construction area in square meters")
    living_area_m2: Optional[float] = Field(None, description="Interior living area in square meters")
    stories: Optional[int] = Field(None, description="Number of floors/stories")
    parking_spaces: Optional[int] = Field(None, description="Number of parking spaces")

    pool: Optional[bool] = Field(None, description="Has swimming pool")
    furnished: Optional[str] = Field(None, description="One of: unfurnished, partially, fully")
    view: Optional[str] = Field(None, description="Primary view type: ocean, mountain, garden, city")
    interior_features: List[str] = Field(default_factory=list, description="Array of interior features")
    exterior_features: List[str] = Field(default_factory=list, description="Array of exterior features")

    list_agent_name: Optional[str] = Field(None, description="Listing agent or contact name")
    list_office_name: Optional[str] = Field(None, description="Real estate agency name")

    extra: dict = Field(default_factory=dict, description="Flat key-value object for any additional data. All values are strings.")

    photos: List[HubPhoto] = Field(default_factory=list, description="Array of photo objects, ordered by display priority")
    videos: List[HubVideo] = Field(default_factory=list, description="Array of video objects")
    virtual_tours: List[HubVirtualTour] = Field(default_factory=list, description="Array of virtual tour objects")

    class Config:
        from_attributes = True


class HubListingsResponse(BaseModel):
    """Root envelope — exact client format."""
    source: str = Field(..., description="Portal identifier. Lowercase, no spaces. Example: pincali, inmuebles24")
    scraped_at: str = Field(..., description="Timestamp of when the scrape was executed. ISO 8601 UTC.")
    listings: List[HubListing] = Field(..., description="Array of listing objects")

    class Config:
        from_attributes = True


# ──────────────────────────────────────────────────────────────
# Field Mapping Helpers
# ──────────────────────────────────────────────────────────────

def _to_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return []


def _to_extra(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    return {}


def build_hub_listing(db_listing: Listing) -> HubListing:
    photos = []
    if db_listing.photos:
        for p in db_listing.photos:
            photos.append(HubPhoto(
                url=p.url,
                position=p.position,
                is_primary=p.is_primary if p.is_primary is not None else (p.position == 0),
                caption=getattr(p, "caption", None),
            ))

    videos = []
    if db_listing.videos:
        for v in db_listing.videos:
            videos.append(HubVideo(
                url=v.url,
                title=getattr(v, "title", None),
            ))

    virtual_tours = []
    if db_listing.virtual_tours:
        for vt in db_listing.virtual_tours:
            virtual_tours.append(HubVirtualTour(
                url=vt.url,
                title=getattr(vt, "title", None),
            ))

    return HubListing(
        external_id=db_listing.external_id,
        origin_url=db_listing.origin_url or "",
        name=(db_listing.name or "").lower(),
        description=db_listing.description,
        supplement=getattr(db_listing, "supplement", None),
        price=db_listing.price_usd or 0.0,
        mls_status=db_listing.mls_status or "Active",
        property_type=db_listing.property_type or "B",
        city=(db_listing.city or "").lower() if db_listing.city else None,
        mls_area_major=db_listing.state,
        community=getattr(db_listing, "community", None),
        subdivision=getattr(db_listing, "subdivision", None),
        latitude=db_listing.latitude,
        longitude=db_listing.longitude,
        beds_total=db_listing.beds_total,
        baths_full=db_listing.baths_full,
        baths_half=db_listing.baths_half,
        year_built=db_listing.year_built,
        lot_size_m2=db_listing.lot_size_m2,
        construction_m2=db_listing.construction_m2,
        living_area_m2=getattr(db_listing, "living_area_m2", None),
        stories=getattr(db_listing, "stories", None),
        parking_spaces=db_listing.parking_spaces,
        pool=db_listing.pool,
        furnished=db_listing.furnished,
        view=db_listing.view,
        interior_features=_to_list(db_listing.interior_features),
        exterior_features=_to_list(db_listing.exterior_features),
        list_agent_name=db_listing.list_agent_name,
        list_office_name=db_listing.list_office_name,
        extra=_to_extra(db_listing.extra),
        photos=photos,
        videos=videos,
        virtual_tours=virtual_tours,
    )


# ──────────────────────────────────────────────────────────────
# FastAPI Endpoint with full Swagger docs
# ──────────────────────────────────────────────────────────────

@router.get(
    "/listings",
    response_model=HubListingsResponse,
    summary="Get listings in client Hub format",
    description="""
    Returns listings transformed into the exact JSON structure the client's integration expects.
    
    **Key behaviors:**
    - Every field is always present in the response
    - Missing data = `null`, `[]`, or `{}` — never omitted
    - `name` and `city` are automatically lowercased
    - `price` is always in USD (pulled from `price_usd` column)
    - `photos` / `videos` / `virtual_tours` are empty arrays when none exist
    - `extra` is an empty object when none
    - `is_primary` defaults to `position === 0` if NULL in DB
    """,
    response_description="Hub-formatted listings wrapped in source + scraped_at envelope",
    responses={
        200: {
            "description": "Successful response with listings in Hub format",
            "content": {
                "application/json": {
                    "example": {
                        "source": "pincali",
                        "scraped_at": "2026-05-26T01:22:00Z",
                        "listings": [
                            {
                                "external_id": "prop-28491837",
                                "origin_url": "https://www.pincali.com/en/home/28491837",
                                "name": "casa con vista al mar en pedregal",
                                "description": "Hermosa residencia de 3 niveles...",
                                "supplement": None,
                                "price": 1250000.0,
                                "mls_status": "Active",
                                "property_type": "B",
                                "city": "cabo san lucas",
                                "mls_area_major": "pedregal",
                                "community": None,
                                "subdivision": None,
                                "latitude": 22.8793,
                                "longitude": -109.9114,
                                "beds_total": 4,
                                "baths_full": 5,
                                "baths_half": 1,
                                "year_built": 2019,
                                "lot_size_m2": 680.0,
                                "construction_m2": 520.0,
                                "living_area_m2": 450.0,
                                "stories": 3,
                                "parking_spaces": 3,
                                "pool": True,
                                "furnished": "fully",
                                "view": "ocean",
                                "interior_features": ["air conditioning", "marble floors"],
                                "exterior_features": ["infinity pool", "terrace"],
                                "list_agent_name": "maria garcia",
                                "list_office_name": "snell real estate",
                                "extra": {"hoa_fee": "500", "hoa_frequency": "monthly"},
                                "photos": [
                                    {
                                        "url": "https://cdn.example.com/photo1.jpg",
                                        "position": 0,
                                        "is_primary": True,
                                        "caption": "Fachada principal"
                                    }
                                ],
                                "videos": [],
                                "virtual_tours": []
                            }
                        ]
                    }
                }
            }
        }
    }
)
async def get_hub_listings(
    db: AsyncSession = Depends(get_db),
    source: Optional[str] = Query(None, description="Filter by portal source (e.g. pincali, inmuebles24)"),
    city: Optional[str] = Query(None, description="Filter by city (case-insensitive partial match)"),
    mls_status: Optional[str] = Query(None, description="Active | Pending | Sold | Cancelled"),
    property_type: Optional[str] = Query(None, description="A=Condo, B=House, D=Reservations, E=Land, F=Commercial, H=Fractional, I=Multi-Family"),
    min_price: Optional[float] = Query(None, ge=0, description="Minimum price in USD"),
    max_price: Optional[float] = Query(None, ge=0, description="Maximum price in USD"),
    beds: Optional[int] = Query(None, ge=0, description="Minimum bedrooms"),
    baths: Optional[int] = Query(None, ge=0, description="Minimum bathrooms"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=500, description="Results per page (max 500)"),
):
    # Build base query with eager loading for relations
    stmt = (
        select(Listing)
        .options(
            selectinload(Listing.photos),
            selectinload(Listing.videos),
            selectinload(Listing.virtual_tours),
        )
    )

    # Apply filters
    if source:
        stmt = stmt.where(Listing.source.ilike(source))
    if city:
        stmt = stmt.where(Listing.city.ilike(f"%{city}%"))
    if mls_status:
        stmt = stmt.where(Listing.mls_status == mls_status)
    if property_type:
        stmt = stmt.where(Listing.property_type == property_type)
    if min_price is not None:
        stmt = stmt.where(Listing.price_usd >= min_price)
    if max_price is not None:
        stmt = stmt.where(Listing.price_usd <= max_price)
    if beds is not None:
        stmt = stmt.where(Listing.beds_total >= beds)
    if baths is not None:
        stmt = stmt.where(Listing.baths_full >= baths)

    # Count total (separate query)
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total_result = await db.execute(count_stmt)
    total = total_result.scalar_one()

    # Pagination
    offset = (page - 1) * page_size
    stmt = stmt.offset(offset).limit(page_size)

    # Execute
    result = await db.execute(stmt)
    db_listings = result.scalars().unique().all()

    if not db_listings:
        return HubListingsResponse(
            source=source or "pincali",
            scraped_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            listings=[],
        )

    hub_listings = [build_hub_listing(l) for l in db_listings]
    response_source = source or db_listings[0].source or "pincali"

    return HubListingsResponse(
        source=response_source.lower().replace(" ", ""),
        scraped_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        listings=hub_listings,
    )