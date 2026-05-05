"""pre-create lightrag full-KV tables to kill the cold-start race

When api + worker both run ``_ensure_lightrag_initialized`` for a fresh
tenant concurrently, both fall into lightrag's
``_migrate_create_full_entities_relations_tables`` which is
SELECT-then-CREATE — not idempotent at PG level. One process wins; the
losers log an ERROR for ``relation "lightrag_full_entities" already
exists`` (and the matching relations table). Function-level the behaviour
is fine, but the noise pollutes ops dashboards and masks real errors.

These two tables are workspace-partitioned at the row level — they are
**tenant-agnostic at the schema level** — so we own creation here and
trim the race entirely. Lightrag's own helper still tries to create them
on every cold-start; the matching downgrade in ``rag_factory`` swallows
the now-expected "already exists" error.

DDL is verbatim from
``raganything/.venv/.../lightrag/kg/postgres_impl.py`` (TABLES dict near
line 6418), wrapped in ``CREATE TABLE IF NOT EXISTS`` plus
``IF NOT EXISTS`` indexes so the migration is itself idempotent against
DBs that already have the tables.

Revision ID: 005
Revises: 004
Create Date: 2026-05-05
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op


revision: str = "005"
down_revision: Union[str, Sequence[str], None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS LIGHTRAG_FULL_ENTITIES (
            id VARCHAR(255),
            workspace VARCHAR(255),
            entity_names JSONB,
            count INTEGER,
            create_time TIMESTAMP(0) DEFAULT CURRENT_TIMESTAMP,
            update_time TIMESTAMP(0) DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT LIGHTRAG_FULL_ENTITIES_PK PRIMARY KEY (workspace, id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_lightrag_full_entities_id "
        "ON LIGHTRAG_FULL_ENTITIES(id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_lightrag_full_entities_workspace_id "
        "ON LIGHTRAG_FULL_ENTITIES(workspace, id)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS LIGHTRAG_FULL_RELATIONS (
            id VARCHAR(255),
            workspace VARCHAR(255),
            relation_pairs JSONB,
            count INTEGER,
            create_time TIMESTAMP(0) DEFAULT CURRENT_TIMESTAMP,
            update_time TIMESTAMP(0) DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT LIGHTRAG_FULL_RELATIONS_PK PRIMARY KEY (workspace, id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_lightrag_full_relations_id "
        "ON LIGHTRAG_FULL_RELATIONS(id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_lightrag_full_relations_workspace_id "
        "ON LIGHTRAG_FULL_RELATIONS(workspace, id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS LIGHTRAG_FULL_ENTITIES CASCADE")
    op.execute("DROP TABLE IF EXISTS LIGHTRAG_FULL_RELATIONS CASCADE")
