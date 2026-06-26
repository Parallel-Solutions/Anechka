"""contact normalized tables

Revision ID: 7e5a2b9c1d34
Revises: 6d4f1a7c9e21
Create Date: 2026-06-26 03:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

revision: str = "7e5a2b9c1d34"
down_revision: Union[str, None] = "6d4f1a7c9e21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "crm_contacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("portal_id", sa.String(length=255), nullable=False),
        sa.Column("contact_id", sa.BigInteger(), nullable=False),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_lead_id", sa.BigInteger(), nullable=True),
        sa.Column("last_name", sa.String(length=255), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("second_name", sa.String(length=255), nullable=True),
        sa.Column("full_name", sa.String(length=512), nullable=True),
        sa.Column("post", sa.String(length=512), nullable=True),
        sa.Column("post_custom", sa.String(length=512), nullable=True),
        sa.Column("company_id", sa.BigInteger(), nullable=True),
        sa.Column("company_title", sa.String(length=512), nullable=True),
        sa.Column("primary_phone", sa.String(length=64), nullable=True),
        sa.Column("primary_phone_type", sa.String(length=16), nullable=True),
        sa.Column("raw_payload", JSONType, nullable=True),
        sa.Column("first_imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("portal_id", "contact_id", name="uq_crm_contact"),
    )
    op.create_index("ix_crm_contacts_company", "crm_contacts", ["portal_id", "company_id"], unique=False)

    op.create_table(
        "crm_contact_phones",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("portal_id", sa.String(length=255), nullable=False),
        sa.Column("contact_id", sa.BigInteger(), nullable=False),
        sa.Column("value", sa.String(length=64), nullable=False),
        sa.Column("value_type", sa.String(length=16), nullable=False, server_default="OTHER"),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("first_imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("portal_id", "contact_id", "value", name="uq_crm_contact_phone"),
    )
    op.create_index(
        "ix_crm_contact_phones_contact", "crm_contact_phones", ["portal_id", "contact_id"], unique=False
    )

    op.create_table(
        "crm_contact_links",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("portal_id", sa.String(length=255), nullable=False),
        sa.Column("contact_id", sa.BigInteger(), nullable=False),
        sa.Column("parent_entity_type_id", sa.Integer(), nullable=False),
        sa.Column("parent_entity_id", sa.BigInteger(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("first_imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "portal_id",
            "contact_id",
            "parent_entity_type_id",
            "parent_entity_id",
            name="uq_crm_contact_link",
        ),
    )
    op.create_index(
        "ix_crm_contact_links_parent",
        "crm_contact_links",
        ["portal_id", "parent_entity_type_id", "parent_entity_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_crm_contact_links_parent", table_name="crm_contact_links")
    op.drop_table("crm_contact_links")
    op.drop_index("ix_crm_contact_phones_contact", table_name="crm_contact_phones")
    op.drop_table("crm_contact_phones")
    op.drop_index("ix_crm_contacts_company", table_name="crm_contacts")
    op.drop_table("crm_contacts")
