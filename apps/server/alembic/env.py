"""Alembic migration environment for the rag_service control plane.

Notes
-----
* Alembic uses **synchronous** SQLAlchemy engines, but the application code
  uses ``postgresql+asyncpg``. We translate the async DSN to a sync one
  (``postgresql+psycopg``) before passing it to the migration engine, so that
  operators can keep a single ``DATABASE_URL`` env var across the app and
  migrations.
* The connection string is read from ``$DATABASE_URL`` rather than from
  ``alembic.ini`` so secrets stay out of version control.
* ``compare_type`` and ``compare_server_default`` are enabled so future
  ``alembic revision --autogenerate`` runs catch column type and server-default
  changes (the default is to ignore both).
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import the declarative metadata so autogenerate has a target schema.
# (``prepend_sys_path = src`` in alembic.ini makes this import resolvable.)
from rag_service.db.base import Base  # noqa: E402

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here for 'autogenerate' support.
target_metadata = Base.metadata


def _resolve_database_url() -> str:
    """Return a sync SQLAlchemy URL derived from ``$DATABASE_URL``.

    Alembic does not support async drivers, so an ``asyncpg`` URL is rewritten
    to ``psycopg`` (SQLAlchemy 2.0 default sync Postgres driver). A bare
    ``postgresql://`` URL is left untouched.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL environment variable is required for alembic "
            "(set e.g. postgresql+asyncpg://user:pass@host/db)"
        )
    if url.startswith("postgresql+asyncpg://"):
        url = "postgresql+psycopg://" + url[len("postgresql+asyncpg://") :]
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine, though an
    Engine is acceptable here as well.  By skipping the Engine creation we
    don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the script output.
    """
    url = _resolve_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine and associate a connection
    with the context.
    """
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _resolve_database_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
