"""ie audit log

Revision ID: 5c3e8d4a2b13
Revises: 4b2d7c1a9f02
Create Date: 2026-06-25 14:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "5c3e8d4a2b13"
down_revision: Union[str, None] = "4b2d7c1a9f02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "ie_audit_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("portal_id", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("object_type", sa.String(length=64), nullable=True),
        sa.Column("object_id", sa.String(length=64), nullable=True),
        sa.Column("detail_json", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ie_audit_log_portal_id", "ie_audit_log", ["portal_id"])
    op.create_index("ix_ie_audit_log_user_id", "ie_audit_log", ["user_id"])
    op.create_index("ix_ie_audit_log_action", "ie_audit_log", ["action"])
    op.create_index("ix_ie_audit_lookup", "ie_audit_log", ["portal_id", "action", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_ie_audit_lookup", table_name="ie_audit_log")
    op.drop_index("ix_ie_audit_log_action", table_name="ie_audit_log")
    op.drop_index("ix_ie_audit_log_user_id", table_name="ie_audit_log")
    op.drop_index("ix_ie_audit_log_portal_id", table_name="ie_audit_log")
    op.drop_table("ie_audit_log")
