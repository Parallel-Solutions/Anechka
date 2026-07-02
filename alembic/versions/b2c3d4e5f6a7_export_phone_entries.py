"""Export phone registry for Tomoru call-result matching

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-01 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "export_phone_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("portal_id", sa.String(length=255), nullable=False),
        sa.Column("export_job_id", sa.Integer(), nullable=False),
        sa.Column("phone_hash", sa.String(length=64), nullable=False),
        sa.Column("normalized_phone", sa.String(length=32), nullable=False),
        sa.Column("deal_id", sa.BigInteger(), nullable=False),
        sa.Column("contact_id", sa.BigInteger(), nullable=True),
        sa.Column("export_mode", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["export_job_id"], ["export_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("export_job_id", "phone_hash", name="uq_export_phone_entries_job_hash"),
    )
    op.create_index("ix_export_phone_entries_portal_id", "export_phone_entries", ["portal_id"])
    op.create_index("ix_export_phone_entries_export_job_id", "export_phone_entries", ["export_job_id"])
    op.create_index("ix_export_phone_entries_phone_hash", "export_phone_entries", ["phone_hash"])
    op.create_index(
        "ix_export_phone_entries_portal_hash",
        "export_phone_entries",
        ["portal_id", "phone_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_export_phone_entries_portal_hash", table_name="export_phone_entries")
    op.drop_index("ix_export_phone_entries_phone_hash", table_name="export_phone_entries")
    op.drop_index("ix_export_phone_entries_export_job_id", table_name="export_phone_entries")
    op.drop_index("ix_export_phone_entries_portal_id", table_name="export_phone_entries")
    op.drop_table("export_phone_entries")
