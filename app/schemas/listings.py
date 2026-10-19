"""
app/schemas/listings.py — Pydantic schemas for API request/response.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_serializer


# ── Media schemas ────────────────────────────────────────────
class PhotoSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    url: str
    position: int
    is_primary: bool
    caption: Optional[str] = None


class VideoSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    url: str
    title: Optional[str] = None


class VirtualTourSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    url: str
    title: Optional[str] = None


# ── Listing schemas ──────────────────────────────────────────
class ListingBase(BaseModel):
    external_id: str
    source: str = "pincali"
    origin_url: str
    name: Optional[str] = None
    description: Optional[str] = None
    supplement: Optional[str] = None
    price_usd: Optional[float] = None
    original_price: Optional[float] = None
    original_currency: Optional[str] = None
    mls_status: Optional[str] = None
    property_type: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = "Mexico"
    community: Optional[str] = None
    subdivision: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    beds_total: Optional[int] = None
    baths_full: Optional[int] = None
    baths_half: Optional[int] = None
    year_built: Optional[int] = None
    lot_size_m2: Optional[float] = None
    construction_m2: Optional[float] = None
    living_area_m2: Optional[float] = None
    stories: Optional[int] = None
    parking_spaces: Optional[int] = None
    pool: Optional[bool] = None
    furnished: Optional[str] = None
    view: Optional[str] = None
    interior_features: Optional[List[str]] = None
    exterior_features: Optional[List[str]] = None
    list_agent_name: Optional[str] = None
    list_office_name: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None


class ListingResponse(ListingBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    photos: List[PhotoSchema] = []
    videos: List[VideoSchema] = []
    virtual_tours: List[VirtualTourSchema] = []
    created_at: datetime
    updated_at: datetime
    last_seen_at: Optional[datetime] = None


class ListingListResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    external_id: str
    source: str
    origin_url: str
    name: Optional[str] = None
    price_usd: Optional[float] = None
    mls_status: Optional[str] = None
    property_type: Optional[str] = None
    city: Optional[str] = None
    beds_total: Optional[int] = None
    baths_full: Optional[int] = None
    lot_size_m2: Optional[float] = None
    construction_m2: Optional[float] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    primary_photo: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    @field_serializer("updated_at", "created_at")
    def serialize_dt(self, v: datetime) -> Optional[str]:
        """Always return ISO 8601 with seconds: 2024-05-12T14:32:07"""
        if v is None:
            return None
        return v.strftime("%Y-%m-%dT%H:%M:%S")


# ── Pagination ───────────────────────────────────────────────
class PaginatedListings(BaseModel):
    total: int
    page: int
    page_size: int
    pages: int
    items: List[ListingListResponse]


# ── Filters ──────────────────────────────────────────────────
class ListingFilters(BaseModel):
    city: Optional[str] = None
    state: Optional[str] = None
    community: Optional[str] = None
    subdivision: Optional[str] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    beds: Optional[int] = None
    baths: Optional[int] = None
    property_type: Optional[str] = None
    mls_status: Optional[str] = None
    sort_by: str = "created_at"
    sort_dir: str = "desc"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


# ── Statistics ───────────────────────────────────────────────
class StatisticsResponse(BaseModel):
    total_listings: int
    active_listings: int
    pending_urls: int
    failed_urls: int
    scraped_urls: int
    listings_by_status: Dict[str, int]
    listings_by_type: Dict[str, int]
    listings_by_city: Dict[str, int]
    avg_price_usd: Optional[float]
    last_run_at: Optional[datetime]


# ── Scraper status ───────────────────────────────────────────
class ScraperStatusResponse(BaseModel):
    status: str
    pending_urls: int
    failed_urls: int
    total_listings: int
    last_run: Optional[Dict[str, Any]]
    recent_errors: int


# ── Search ───────────────────────────────────────────────────
class SearchResponse(BaseModel):
    query: str
    total: int
    items: List[ListingListResponse]


# ── URL schemas ──────────────────────────────────────────────
class ListingURLResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    url: str
    status: str
    external_id: Optional[str]
    discovered_at: datetime
    last_scraped_at: Optional[datetime]
    scrape_attempts: int
    error_message: Optional[str]


# ── Geography catalog ─────────────────────────────────────────
class GeoStateEntry(BaseModel):
    state: str


class GeoCityEntry(BaseModel):
    state: Optional[str] = None
    city: str


class GeoCommunityEntry(BaseModel):
    state: Optional[str] = None
    city: Optional[str] = None
    community: str


class GeoSubdivisionEntry(BaseModel):
    state: Optional[str] = None
    city: Optional[str] = None
    community: Optional[str] = None
    subdivision: str


# ── Paginated wrappers for each geo level ─────────────────────
class GeoPaginatedStates(BaseModel):
    items: List[GeoStateEntry]
    total: int
    page: int
    page_size: int
    pages: int


class GeoPaginatedCities(BaseModel):
    items: List[GeoCityEntry]
    total: int
    page: int
    page_size: int
    pages: int


class GeoPaginatedCommunities(BaseModel):
    items: List[GeoCommunityEntry]
    total: int
    page: int
    page_size: int
    pages: int


class GeoPaginatedSubdivisions(BaseModel):
    items: List[GeoSubdivisionEntry]
    total: int
    page: int
    page_size: int
    pages: int


class GeographyCatalogResponse(BaseModel):
    """
    Paginated geographic hierarchy catalog.

    All four levels share the same ``page`` / ``page_size`` window.
    Use the ``state`` / ``city`` / ``community`` filters to drill down
    into a specific branch of the hierarchy without fetching everything.
    """

    states: GeoPaginatedStates
    cities: GeoPaginatedCities
    communities: GeoPaginatedCommunities
    subdivisions: GeoPaginatedSubdivisions


# ── Raw Address ───────────────────────────────────────────────
class ParsedLocationFields(BaseModel):
    """The normalizer's parsed output for a single listing."""
    subdivision: Optional[str] = None
    community: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class RawAddressResponse(BaseModel):
    """
    Raw location strings alongside parsed geo fields for one listing.
    Use this to verify that the parser extracted the right values from
    the original address text.
    """
    listing_id: int
    external_id: str
    origin_url: str
    raw_address: Optional[str] = Field(
        None,
        description=(
            "The full, unsanitised address string scraped from the "
            "listing's map/location section. Right-to-left hierarchy: "
            "last segment = state, second-to-last = city, etc."
        ),
    )
    location_subtitle_raw: Optional[str] = Field(
        None,
        description=(
            "The raw h2.location subtitle from the listing page "
            "(e.g. 'Casa en Venta en Pachuca de Soto, Hidalgo')."
        ),
    )
    parsed: ParsedLocationFields = Field(
        default_factory=ParsedLocationFields,
        description="Parsed geographic fields stored in the DB.",
    )


class PaginatedRawAddresses(BaseModel):
    total: int
    page: int
    page_size: int
    pages: int
    items: List[RawAddressResponse]