"""baseline

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-16 00:00:00

This is the **baseline** revision. It exists as an anchor so every future
``alembic revision --autogenerate`` has something to diff against.

Why both ``upgrade()`` and ``downgrade()`` are empty:

The schema captured by ``app/models.py`` is already on every existing
database — dev SQLite files and the prod ``data/wages.db`` were both
materialized by ``Base.metadata.create_all`` inside ``app.db.init_db()``
on first boot. Re-running those ``CREATE TABLE`` statements here would
either be a no-op (best case) or fight subtle DDL differences between
SQLAlchemy's ``create_all`` output and Alembic's ``op.create_table``
output (worst case).

So: bootstrap a fresh deployment by running ``alembic stamp head`` once,
which records this revision in ``alembic_version`` without running the
empty migration. From then on, every model change ships as its own
``alembic revision --autogenerate`` migration, which IS applied normally.

See the "Schema migrations" section in README.md for the operator workflow.
"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Intentionally empty — see module docstring.
    pass


def downgrade() -> None:
    # Intentionally empty — baseline has no predecessor to revert to.
    pass
