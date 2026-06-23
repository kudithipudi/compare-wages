"""drop unused columns: classification_confidence, bus_ratio, cbsa_kind

Revision ID: 0004_drop_unused_columns
Revises: 0003_add_role_discovery_suggestions
Create Date: 2026-06-22 00:00:00

Cleanup pass from the senior-architect review. These columns are written but
never read anywhere downstream:

- ``job_postings.classification_confidence`` — set by the LLM classify step,
  but no template, no service, no filter ever consults it.
- ``zip_cbsa.bus_ratio`` — populated from the HUD crosswalk row that wins
  the highest-bus-ratio dedupe at load time, then never re-read.
- ``cbsa_names.cbsa_kind`` — ``metro``/``micro``/``other`` discriminator that
  no template renders.

Rejected from the original drop list after a second audit:

- ``competitors.source_priority`` — actively used by ``admin/competitors.html``
  (renders as a ``P{n}`` pill) and the listing query orders by it. Operator
  surface, keep.
- ``bls_oews_wages.{p10, p25, p75, p90, mean_hourly}`` — p25/p75/mean_hourly
  are displayed in the per-state BLS table on the location detail page.
  Keep.

SQLite path: ``op.batch_alter_table`` is required because ``DROP COLUMN``
landed in SQLite 3.35 (2021) and Alembic's batch mode handles older runtimes
by copying the table. env.py already enables ``render_as_batch=True``.
"""
from __future__ import annotations

from alembic import op


revision = "0004_drop_unused_columns"
down_revision = "0003_add_role_discovery_suggestions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("job_postings") as batch_op:
        batch_op.drop_column("classification_confidence")

    with op.batch_alter_table("zip_cbsa") as batch_op:
        batch_op.drop_column("bus_ratio")

    with op.batch_alter_table("cbsa_names") as batch_op:
        batch_op.drop_column("cbsa_kind")


def downgrade() -> None:
    import sqlalchemy as sa

    with op.batch_alter_table("job_postings") as batch_op:
        batch_op.add_column(sa.Column("classification_confidence", sa.Float(), nullable=True))

    with op.batch_alter_table("zip_cbsa") as batch_op:
        batch_op.add_column(sa.Column("bus_ratio", sa.Float(), nullable=False, server_default="0.0"))

    with op.batch_alter_table("cbsa_names") as batch_op:
        batch_op.add_column(sa.Column("cbsa_kind", sa.String(), nullable=False, server_default="metro"))
