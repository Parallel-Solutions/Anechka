"""Add export filter index on crm_entities

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-02 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_crm_entities_export_filter",
        "crm_entities",
        ["portal_id", "entity_type_id", "category_id", "created_time", "entity_id"],
        unique=False,
        postgresql_where="is_deleted = false",
    )


def downgrade() -> None:
    op.drop_index("ix_crm_entities_export_filter", table_name="crm_entities")
