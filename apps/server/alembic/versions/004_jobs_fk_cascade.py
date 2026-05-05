"""jobs.document_id FK -> ON DELETE CASCADE

Soft-deleting a Document flips ``status='deleted'``; the periodic
``rebuild_index`` worker then issues a hard ``DELETE FROM documents
WHERE status='deleted'``. The original 001 migration declared
``jobs.document_id`` as a FK to ``documents.document_id`` without an
``ondelete`` clause, so Postgres rejects the cascade hard-delete with a
``ForeignKeyViolationError`` and the rebuild silently retries forever.

Job rows for deleted documents are scratch state — the document table is
the system-of-record. Cascading them away is the desired semantic.

Revision ID: 004
Revises: 003
Create Date: 2026-05-05
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from alembic import op


revision: str = "004"
down_revision: Union[str, Sequence[str], None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("jobs_document_id_fkey", "jobs", type_="foreignkey")
    op.create_foreign_key(
        "jobs_document_id_fkey",
        source_table="jobs",
        referent_table="documents",
        local_cols=["document_id"],
        remote_cols=["document_id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("jobs_document_id_fkey", "jobs", type_="foreignkey")
    op.create_foreign_key(
        "jobs_document_id_fkey",
        source_table="jobs",
        referent_table="documents",
        local_cols=["document_id"],
        remote_cols=["document_id"],
    )
