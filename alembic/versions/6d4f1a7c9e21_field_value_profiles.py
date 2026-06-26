"""field value profiles + discovered_from_payload

Revision ID: 6d4f1a7c9e21
Revises: 5c3e8d4a2b13
Create Date: 2026-06-26 02:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

revision: str = "6d4f1a7c9e21"
down_revision: Union[str, None] = "5c3e8d4a2b13"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")


def upgrade() -> None:
    op.add_column(
        "crm_field_definitions",
        sa.Column("discovered_from_payload", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "crm_field_value_profiles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("portal_id", sa.String(length=255), nullable=False),
        sa.Column("entity_type_id", sa.Integer(), nullable=False),
        sa.Column("field_code", sa.String(length=255), nullable=False),
        sa.Column("field_definition_id", sa.Integer(), nullable=True),
        sa.Column("observed_types", JSONType, nullable=True),
        sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("filled_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("null_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("distinct_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sample_values", JSONType, nullable=True),
        sa.Column("numeric_stats", JSONType, nullable=True),
        sa.Column("length_stats", JSONType, nullable=True),
        sa.Column("value_signature", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("portal_id", "entity_type_id", "field_code", name="uq_crm_field_value_profile"),
    )
    op.create_index("ix_crm_fvp_def", "crm_field_value_profiles", ["field_definition_id"], unique=False)
    op.create_index("ix_crm_fvp_sig", "crm_field_value_profiles", ["value_signature"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_crm_fvp_sig", table_name="crm_field_value_profiles")
    op.drop_index("ix_crm_fvp_def", table_name="crm_field_value_profiles")
    op.drop_table("crm_field_value_profiles")
    op.drop_column("crm_field_definitions", "discovered_from_payload")
