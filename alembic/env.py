"""Alembic migration environment for compare-wages.

Wires alembic to the same SQLAlchemy metadata the app uses:

- ``target_metadata`` points at ``app.db.Base.metadata``.
- We explicitly ``import app.models`` so every ORM class registers itself on
  ``Base.metadata`` before ``--autogenerate`` diffs the database. Without this
  import, autogenerate would think every table needs to be dropped.
- ``sqlalchemy.url`` is sourced from the ``DATABASE_URL`` env var with the
  same default as ``app.config.Settings`` — one URL convention across dev,
  prod, and ``alembic`` invocations.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# Make the project importable when alembic is invoked from the project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Import Base + register every ORM model so target_metadata is populated.
from app.db import Base  # noqa: E402
from app import models  # noqa: F401,E402  side-effect: registers tables on Base.metadata

# Alembic Config object — provides access to .ini values.
config = context.config

# Resolve the database URL: env var wins, otherwise fall back to the same
# default the app uses. Don't rely on alembic.ini's empty `sqlalchemy.url`.
_DEFAULT_DATABASE_URL = "sqlite:///./data/wages.db"
_database_url = os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)
config.set_main_option("sqlalchemy.url", _database_url)

# Interpret the config file for Python logging when one is provided.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emits SQL without a live DB connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite needs batch mode for most ALTER operations.
        render_as_batch=_is_sqlite(url or ""),
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — opens an Engine and a Connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite can't ALTER COLUMN; batch mode rewrites the table instead.
            render_as_batch=_is_sqlite(str(connectable.url)),
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
