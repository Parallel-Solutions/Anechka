"""Add Tomoru dispatch metadata to the retry queue.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-23 10:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "call_retry_queue_entries",
        sa.Column("phone_extension", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "call_retry_queue_entries",
        sa.Column("dispatched_campaign_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "call_retry_queue_entries",
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("call_retry_queue_entries", "dispatched_at")
    op.drop_column("call_retry_queue_entries", "dispatched_campaign_id")
    op.drop_column("call_retry_queue_entries", "phone_extension")
