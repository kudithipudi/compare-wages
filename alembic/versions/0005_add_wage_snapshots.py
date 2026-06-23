"""add wage_snapshots table

Revision ID: 0005_add_wage_snapshots
Revises: 0004_drop_unused_columns
Create Date: 2026-06-22 00:00:00

Adds a per-(yard, run) point-in-time record so the dashboard can show wage
drift over time. Written by the ingestion orchestrator at the end of every
run. The exec overview reads recent snapshots to render a sparkline strip;
the yard-detail page renders a 12-week mini-chart with a "+$X vs N wks ago"
delta chip.

One row per (yard, run) is the right grain: a single national run produces
~73 (active-yard) snapshots per run. At weekly scheduling that's ~3.8K rows
per year — trivial for SQLite. We do NOT add a unique constraint on
(yard_id, captured_at) because two manual runs on the same day are a real
operator workflow and we want both observations.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0005_add_wage_snapshots"
down_revision = "0004_drop_unused_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wage_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("yard_id", sa.Integer(), sa.ForeignKey("copart_locations.id"), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("copart_wage", sa.Float(), nullable=False),
        sa.Column("blended_competitive_wage", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("gap", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("observation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pressure_quartile", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_wage_snapshots_yard_id", "wage_snapshots", ["yard_id"])
    op.create_index("ix_wage_snapshots_captured_at", "wage_snapshots", ["captured_at"])


def downgrade() -> None:
    op.drop_index("ix_wage_snapshots_captured_at", table_name="wage_snapshots")
    op.drop_index("ix_wage_snapshots_yard_id", table_name="wage_snapshots")
    op.drop_table("wage_snapshots")
