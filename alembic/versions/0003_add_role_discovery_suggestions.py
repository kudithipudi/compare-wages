"""add role_discovery_suggestions table

Revision ID: 0003_add_role_discovery_suggestions
Revises: 0002_add_extraction_no_wage_found
Create Date: 2026-06-20 00:00:00

Adds a parking-lot table for the Role Discovery admin workflow. The scraper's
keyword set is derived from ``role_mappings.competitor_role`` — every new keyword
expands coverage. Operators currently add them one form at a time on
``/admin/role-mappings``. Role Discovery mines ``job_postings.raw_title`` values
that were pulled in incidentally (search results overspill), LLM-classifies them
into ``outdoor`` / ``indoor`` / ``not_relevant`` buckets, and writes the
candidates here as pending suggestions. The operator reviews each in the new
``/admin/role-discovery`` UI; accepting writes a ``RoleMapping`` row that the
next scrape will use.

The ``UNIQUE (competitor_id, raw_title)`` constraint enables idempotent re-runs:
the orchestrator UPDATEs an existing pending row instead of inserting a duplicate
(crash) and skips anything already accepted/rejected so a stale title can't keep
re-appearing.

The ``source`` column is forward-looking — V1 always writes
``'existing_postings'`` (mining the DB). A future V2 ``'careers_search'`` source
would actively browse the employer's careers page and surface adjacent titles
without needing them in the DB first.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0003_add_role_discovery_suggestions"
down_revision: Union[str, None] = "0002_add_extraction_no_wage_found"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "role_discovery_suggestions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "competitor_id",
            sa.Integer(),
            sa.ForeignKey("competitors.id"),
            nullable=False,
        ),
        sa.Column("raw_title", sa.String(), nullable=False),
        sa.Column("suggested_bucket", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("reasoning", sa.String(), nullable=False, server_default=""),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "source",
            sa.String(),
            nullable=False,
            server_default="existing_postings",
        ),
        sa.UniqueConstraint(
            "competitor_id", "raw_title", name="uq_role_discovery_comp_title"
        ),
    )
    op.create_index(
        "ix_role_discovery_competitor_id",
        "role_discovery_suggestions",
        ["competitor_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_role_discovery_competitor_id",
        table_name="role_discovery_suggestions",
    )
    op.drop_table("role_discovery_suggestions")
