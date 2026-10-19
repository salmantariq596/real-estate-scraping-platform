"""Initial schema

Revision ID: 001_initial
Revises: 
Create Date: 2025-01-01 00:00:00

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, mysql

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # listing_urls
    op.create_table(
        "listing_urls",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("external_id", sa.String(255), nullable=True),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("source", sa.String(64), nullable=False, server_default="pincali"),
        sa.Column("filter_name", sa.String(255), nullable=True),
        sa.Column("filter_id", sa.Integer(), nullable=True),
        sa.Column("sort_method", sa.String(32), nullable=True),
        sa.Column("discovered_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("last_scraped_at", sa.DateTime(), nullable=True),
        sa.Column("scrape_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.String(1024), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url", name="uq_listing_urls_url"),
    )
    op.create_index("ix_listing_urls_status", "listing_urls", ["status"])
    op.create_index("ix_listing_urls_discovered_at", "listing_urls", ["discovered_at"])
    op.create_index("ix_listing_urls_external_id", "listing_urls", ["external_id"])

    # listings
    op.create_table(
        "listings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("source", sa.String(64), nullable=False, server_default="pincali"),
        sa.Column("origin_url", sa.String(2048), nullable=False),
        sa.Column("name", sa.String(512), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("supplement", sa.Text(), nullable=True),
        sa.Column("price_usd", sa.Float(), nullable=True),
        sa.Column("original_price", sa.Float(), nullable=True),
        sa.Column("original_currency", sa.String(8), nullable=True),
        sa.Column("mls_status", sa.String(32), nullable=True),
        sa.Column("property_type", sa.String(32), nullable=True),
        sa.Column("city", sa.String(128), nullable=True),
        sa.Column("state", sa.String(128), nullable=True),
        sa.Column("country", sa.String(64), nullable=True, server_default="Mexico"),
        sa.Column("community", sa.String(128), nullable=True),
        sa.Column("subdivision", sa.String(128), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("beds_total", sa.Integer(), nullable=True),
        sa.Column("baths_full", sa.Integer(), nullable=True),
        sa.Column("baths_half", sa.Integer(), nullable=True),
        sa.Column("year_built", sa.Integer(), nullable=True),
        sa.Column("lot_size_m2", sa.Float(), nullable=True),
        sa.Column("construction_m2", sa.Float(), nullable=True),
        sa.Column("living_area_m2", sa.Float(), nullable=True),
        sa.Column("stories", sa.Integer(), nullable=True),
        sa.Column("parking_spaces", sa.Integer(), nullable=True),
        sa.Column("pool", sa.Boolean(), nullable=True),
        sa.Column("furnished", sa.String(32), nullable=True),
        sa.Column("view", sa.String(128), nullable=True),
        sa.Column("interior_features", sa.JSON(), nullable=True),
        sa.Column("exterior_features", sa.JSON(), nullable=True),
        sa.Column("list_agent_name", sa.String(256), nullable=True),
        sa.Column("list_office_name", sa.String(256), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id", "source", name="uq_listings_ext_source"),
    )
    op.create_index("ix_listings_city", "listings", ["city"])
    op.create_index("ix_listings_price_usd", "listings", ["price_usd"])
    op.create_index("ix_listings_mls_status", "listings", ["mls_status"])
    op.create_index("ix_listings_property_type", "listings", ["property_type"])
    op.create_index("ix_listings_created_at", "listings", ["created_at"])

    # listing_photos
    op.create_table(
        "listing_photos",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.BigInteger(), sa.ForeignKey("listings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("caption", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_listing_photos_listing_id", "listing_photos", ["listing_id"])

    # listing_videos
    op.create_table(
        "listing_videos",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.BigInteger(), sa.ForeignKey("listings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("title", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_listing_videos_listing_id", "listing_videos", ["listing_id"])

    # virtual_tours
    op.create_table(
        "virtual_tours",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.BigInteger(), sa.ForeignKey("listings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("title", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_virtual_tours_listing_id", "virtual_tours", ["listing_id"])

    # scrape_runs
    op.create_table(
        "scrape_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_type", sa.String(32), nullable=False),
        sa.Column("source", sa.String(64), nullable=False, server_default="pincali"),
        sa.Column("status", sa.String(32), nullable=False, server_default="running"),
        sa.Column("urls_discovered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("urls_scraped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("urls_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("listings_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("listings_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scrape_runs_started_at", "scrape_runs", ["started_at"])

    # listing_changes
    op.create_table(
        "listing_changes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("listing_id", sa.BigInteger(), sa.ForeignKey("listings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("field_name", sa.String(128), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("changed_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_listing_changes_listing_id", "listing_changes", ["listing_id"])
    op.create_index("ix_listing_changes_changed_at", "listing_changes", ["changed_at"])

    # scraper_errors
    op.create_table(
        "scraper_errors",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.BigInteger(), sa.ForeignKey("scrape_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("url", sa.String(2048), nullable=True),
        sa.Column("error_type", sa.String(64), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("traceback", sa.Text(), nullable=True),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scraper_errors_created_at", "scraper_errors", ["created_at"])
    op.create_index("ix_scraper_errors_error_type", "scraper_errors", ["error_type"])


def downgrade() -> None:
    for table in [
        "scraper_errors", "listing_changes", "scrape_runs",
        "virtual_tours", "listing_videos", "listing_photos",
        "listings", "listing_urls",
    ]:
        op.drop_table(table)
