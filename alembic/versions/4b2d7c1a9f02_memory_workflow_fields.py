"""memory workflow fields

Revision ID: 4b2d7c1a9f02
Revises: 3a1c9e2f4b01
Create Date: 2026-06-25 13:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "4b2d7c1a9f02"
down_revision: Union[str, None] = "3a1c9e2f4b01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ie_memory_entries", sa.Column("status", sa.String(length=16), nullable=False, server_default="approved"))
    op.add_column("ie_memory_entries", sa.Column("priority", sa.Integer(), nullable=False, server_default="100"))
    op.add_column("ie_memory_entries", sa.Column("source", sa.String(length=16), nullable=False, server_default="manual"))
    op.add_column("ie_memory_entries", sa.Column("source_conversation_id", sa.Integer(), nullable=True))
    op.add_column("ie_memory_entries", sa.Column("source_message_id", sa.Integer(), nullable=True))
    op.add_column("ie_memory_entries", sa.Column("approved_by_user_id", sa.Integer(), nullable=True))
    op.add_column("ie_memory_entries", sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ie_memory_entries", sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ie_memory_entries", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_ie_memory_status",
        "ie_memory_entries",
        ["portal_id", "scope", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ie_memory_status", table_name="ie_memory_entries")
    op.drop_column("ie_memory_entries", "deleted_at")
    op.drop_column("ie_memory_entries", "valid_to")
    op.drop_column("ie_memory_entries", "valid_from")
    op.drop_column("ie_memory_entries", "approved_by_user_id")
    op.drop_column("ie_memory_entries", "source_message_id")
    op.drop_column("ie_memory_entries", "source_conversation_id")
    op.drop_column("ie_memory_entries", "source")
    op.drop_column("ie_memory_entries", "priority")
    op.drop_column("ie_memory_entries", "status")
