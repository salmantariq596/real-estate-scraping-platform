"""Widen original_currency from VARCHAR(8) to VARCHAR(16)

Revision ID: 002_widen_currency
Revises: 001_initial
Create Date: 2026-06-09

Problem: The original_currency column was defined as VARCHAR(8), which is too
narrow for raw currency strings the scraper may pull from the DOM (e.g.
"Pesos Mexicanos", "Mexican Peso"). This caused INSERT failures with:
    ERROR: value too long for type character varying(8)

Fix: The normalizer now maps all raw currency text to ISO 4217 codes (MXN/USD),
which are only 3 chars. VARCHAR(16) gives comfortable headroom for any edge case.
"""
from alembic import op
import sqlalchemy as sa

revision = "002_widen_currency"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "listings",
        "original_currency",
        existing_type=sa.String(8),
        type_=sa.String(16),
        existing_nullable=True,
    )


def downgrade() -> None:
    # Note: downgrade will fail if any row contains a value longer than 8 chars.
    # Truncate manually if needed before running downgrade.
    op.alter_column(
        "listings",
        "original_currency",
        existing_type=sa.String(16),
        type_=sa.String(8),
        existing_nullable=True,
    )
