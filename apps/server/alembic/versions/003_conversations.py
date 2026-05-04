"""conversation tables (conversations, messages)

Adds chat history tables on top of the 002 auth tables:
  - conversations  (FK tenant CASCADE, FK user SET NULL)
  - messages       (FK conversation CASCADE, JSONB sources)

Revision ID: 003
Revises: 002
Create Date: 2026-05-04
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "003"
down_revision: Union[str, Sequence[str], None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create conversations and messages tables."""

    # ---- conversations -----------------------------------------------------
    op.create_table(
        "conversations",
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "conversations_tenant_user_updated_idx",
        "conversations",
        ["tenant_id", "user_id", sa.text("updated_at DESC")],
    )

    # ---- messages ----------------------------------------------------------
    op.create_table(
        "messages",
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "sources",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.conversation_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "messages_conversation_created_idx",
        "messages",
        ["conversation_id", "created_at"],
    )


def downgrade() -> None:
    """Drop conversation tables in reverse dependency order."""
    op.drop_index("messages_conversation_created_idx", table_name="messages")
    op.drop_table("messages")

    op.drop_index(
        "conversations_tenant_user_updated_idx", table_name="conversations"
    )
    op.drop_table("conversations")
