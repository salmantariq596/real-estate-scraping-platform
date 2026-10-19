"""
app/models/orm.py — SQLAlchemy ORM models for all database tables.
Compatible with both PostgreSQL and MySQL via generic column types.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# JSON type that works for both PG and MySQL
from sqlalchemy import JSON

# BIGINT primary key on PostgreSQL/MySQL; INTEGER on SQLite so tests get autoincrement
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# listing_urls — URL discovery queue
# ─────────────────────────────────────────────────────────────────────────────
class ListingURL(Base):
    __tablename__ = "listing_urls"
    __table_args__ = (
        UniqueConstraint("url", name="uq_listing_urls_url"),
        Index("ix_listing_urls_discovered_at", "discovered_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", index=True
    )  # pending | scraping | done | failed | skipped
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="pincali")
    filter_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    filter_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sort_method: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    last_scraped_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    scrape_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ─────────────────────────────────────────────────────────────────────────────
# listings — main property table
# ─────────────────────────────────────────────────────────────────────────────
class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("external_id", "source", name="uq_listings_ext_source"),
        Index("ix_listings_city", "city"),
        Index("ix_listings_price_usd", "price_usd"),
        Index("ix_listings_mls_status", "mls_status"),
        Index("ix_listings_property_type", "property_type"),
        Index("ix_listings_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="pincali")
    origin_url: Mapped[str] = mapped_column(String(2048), nullable=False)

    # Content
    name: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    supplement: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Price
    price_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    original_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    original_currency: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    # Status
    mls_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    property_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # Location
    city: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    state: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, default="Mexico")
    community: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    subdivision: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Property details
    beds_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    baths_full: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    baths_half: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    year_built: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    lot_size_m2: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    construction_m2: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    living_area_m2: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stories: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    parking_spaces: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pool: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    furnished: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    view: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # Features (stored as JSON arrays)
    interior_features: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    exterior_features: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Agent
    list_agent_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    list_office_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    # Extra catch-all JSON
    extra: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Relationships
    photos: Mapped[list["ListingPhoto"]] = relationship(
        "ListingPhoto", back_populates="listing", cascade="all, delete-orphan"
    )
    videos: Mapped[list["ListingVideo"]] = relationship(
        "ListingVideo", back_populates="listing", cascade="all, delete-orphan"
    )
    virtual_tours: Mapped[list["VirtualTour"]] = relationship(
        "VirtualTour", back_populates="listing", cascade="all, delete-orphan"
    )
    changes: Mapped[list["ListingChange"]] = relationship(
        "ListingChange", back_populates="listing", cascade="all, delete-orphan"
    )


# ─────────────────────────────────────────────────────────────────────────────
# listing_photos
# ─────────────────────────────────────────────────────────────────────────────
class ListingPhoto(Base):
    __tablename__ = "listing_photos"
    __table_args__ = (Index("ix_listing_photos_listing_id", "listing_id"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("listings.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    caption: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    listing: Mapped["Listing"] = relationship("Listing", back_populates="photos")


# ─────────────────────────────────────────────────────────────────────────────
# listing_videos
# ─────────────────────────────────────────────────────────────────────────────
class ListingVideo(Base):
    __tablename__ = "listing_videos"
    __table_args__ = (Index("ix_listing_videos_listing_id", "listing_id"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("listings.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    listing: Mapped["Listing"] = relationship("Listing", back_populates="videos")


# ─────────────────────────────────────────────────────────────────────────────
# virtual_tours
# ─────────────────────────────────────────────────────────────────────────────
class VirtualTour(Base):
    __tablename__ = "virtual_tours"
    __table_args__ = (Index("ix_virtual_tours_listing_id", "listing_id"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("listings.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    listing: Mapped["Listing"] = relationship("Listing", back_populates="virtual_tours")


# ─────────────────────────────────────────────────────────────────────────────
# scrape_runs — audit log for each scraping session
# ─────────────────────────────────────────────────────────────────────────────
class ScrapeRun(Base):
    __tablename__ = "scrape_runs"
    __table_args__ = (Index("ix_scrape_runs_started_at", "started_at"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    run_type: Mapped[str] = mapped_column(String(32), nullable=False)  # discovery | detail
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="pincali")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    # running | completed | failed | cancelled
    urls_discovered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    urls_scraped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    urls_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    listings_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    listings_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


# ─────────────────────────────────────────────────────────────────────────────
# listing_changes — field-level change history
# ─────────────────────────────────────────────────────────────────────────────
class ListingChange(Base):
    __tablename__ = "listing_changes"
    __table_args__ = (
        Index("ix_listing_changes_listing_id", "listing_id"),
        Index("ix_listing_changes_changed_at", "changed_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("listings.id", ondelete="CASCADE"), nullable=False
    )
    field_name: Mapped[str] = mapped_column(String(128), nullable=False)
    old_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    new_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    listing: Mapped["Listing"] = relationship("Listing", back_populates="changes")


# ─────────────────────────────────────────────────────────────────────────────
# scraper_errors — detailed error log
# ─────────────────────────────────────────────────────────────────────────────
class ScraperError(Base):
    __tablename__ = "scraper_errors"
    __table_args__ = (
        Index("ix_scraper_errors_created_at", "created_at"),
        Index("ix_scraper_errors_error_type", "error_type"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    run_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("scrape_runs.id", ondelete="SET NULL"), nullable=True
    )
    url: Mapped[Optional[str]] = mapped_column(String(2048), nullable=True)
    error_type: Mapped[str] = mapped_column(String(64), nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    traceback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    context: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )