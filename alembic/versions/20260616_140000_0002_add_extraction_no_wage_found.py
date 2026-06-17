"""add extraction_no_wage_found to scrape_runs and scraper_runs

Revision ID: 0002_add_extraction_no_wage_found
Revises: 0001_baseline
Create Date: 2026-06-16 14:00:00

Adds a third extraction-outcome counter on both run tables so operators can
distinguish "LLM responded but the page disclosed no wage" (honest data outcome —
Home Depot in non-pay-transparency states) from "LLM transport actually failed"
(real bug — model 4xx/5xx, parse error, network timeout). Previously both were
lumped into ``extraction_failed`` which made operator triage impossible.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002_add_extraction_no_wage_found"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite-safe: server_default of "0" lets us add a NOT NULL column without
    # rewriting every existing row by hand. Postgres handles the same DDL fine.
    op.add_column(
        "scrape_runs",
        sa.Column(
            "extraction_no_wage_found",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "scraper_runs",
        sa.Column(
            "extraction_no_wage_found",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("scraper_runs", "extraction_no_wage_found")
    op.drop_column("scrape_runs", "extraction_no_wage_found")
