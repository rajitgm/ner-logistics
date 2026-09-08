"""Alembic environment.

Two things here are worth knowing before editing:

1. **The URL comes from Settings, never from alembic.ini.** A migration that can
   be pointed at a different database than the running API is a way to corrupt
   production, so the connection string is built by the same configuration object
   the app uses and no credential is ever written to a tracked file.

2. **Migrations run synchronously.** The API uses asyncpg, but Alembic's
   transactional DDL is simpler and more reliable over a plain sync connection,
   so a separate psycopg engine is used here. Both point at the same database.

PostGIS and pgcrypto also add objects of their own to the database — the
``spatial_ref_sys`` table, the ``tiger`` and ``topology`` schemas. Autogenerate
would otherwise propose dropping all of them on the next revision, so they are
filtered out below.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# The api service root, so `app.*` imports resolve when alembic is invoked from
# anywhere. prepend_sys_path in alembic.ini covers the usual case; this covers
# the rest.
API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import app.models  # noqa: E402,F401  (registers every table on Base.metadata)
from app.core.config import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

#: Objects owned by PostGIS, pgcrypto and PostGIS's optional extras. They live in
#: our database but are not ours to migrate.
EXTENSION_TABLES = frozenset(
    {
        "spatial_ref_sys",
        "geography_columns",
        "geometry_columns",
        "raster_columns",
        "raster_overviews",
    }
)
EXTENSION_SCHEMAS = frozenset({"tiger", "tiger_data", "topology"})


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Keep extension-owned objects out of autogenerate diffs."""

    if type_ == "table":
        if name in EXTENSION_TABLES:
            return False
        if getattr(obj, "schema", None) in EXTENSION_SCHEMAS:
            return False
    # GeoAlchemy2 manages spatial indexes itself when spatial_index=True. Ours are
    # created explicitly in the migrations, so anything named *_gist is already
    # accounted for and must not be proposed for deletion.
    if type_ == "index" and name and name.endswith("_gist"):
        return False
    return True


def _database_url() -> str:
    """Synchronous DSN for migrations, from the application settings."""

    return get_settings().sqlalchemy_dsn

def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it.

    Used to hand a reviewable script to a DBA who will not give the application
    DDL rights on a shared cluster — a realistic constraint for a government
    deployment.
    """

    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live database in one transaction."""

    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
            compare_server_default=True,
            # Postgres does transactional DDL, so a failed migration leaves no
            # half-created schema behind.
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

