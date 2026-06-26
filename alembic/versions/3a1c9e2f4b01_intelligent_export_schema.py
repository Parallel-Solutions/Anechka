"""intelligent export schema

Revision ID: 3a1c9e2f4b01
Revises: 2b32241187b3
Create Date: 2026-06-25 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

revision: str = "3a1c9e2f4b01"
down_revision: Union[str, None] = "2b32241187b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "app_users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("portal_id", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("crm_user_external_id", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("portal_id", "email", name="uq_app_user_email"),
    )
    op.create_index("ix_app_users_portal_id", "app_users", ["portal_id"], unique=False)

    op.create_table(
        "ie_conversations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("portal_id", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_plan_version_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ie_conversations_portal_id", "ie_conversations", ["portal_id"], unique=False)
    op.create_index("ix_ie_conversations_user_id", "ie_conversations", ["user_id"], unique=False)
    op.create_index(
        "ix_ie_conversations_user_updated",
        "ie_conversations",
        ["portal_id", "user_id", "updated_at"],
        unique=False,
    )

    op.create_table(
        "ie_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata_json", JSONType, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ie_messages_conversation_id", "ie_messages", ["conversation_id"], unique=False)
    op.create_index(
        "ix_ie_messages_conversation",
        "ie_messages",
        ["conversation_id", "id"],
        unique=False,
    )

    op.create_table(
        "ie_export_plan_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("plan_json", JSONType, nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("validation_result_json", JSONType, nullable=True),
        sa.Column("catalog_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "version_number", name="uq_ie_plan_version"),
    )
    op.create_index(
        "ix_ie_export_plan_versions_conversation_id",
        "ie_export_plan_versions",
        ["conversation_id"],
        unique=False,
    )

    op.create_table(
        "ie_memory_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("portal_id", sa.String(length=255), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("value_json", JSONType, nullable=True),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ie_memory_entries_portal_id", "ie_memory_entries", ["portal_id"], unique=False)
    op.create_index(
        "ix_ie_memory_lookup",
        "ie_memory_entries",
        ["portal_id", "scope", "kind", "key"],
        unique=False,
    )

    op.create_table(
        "ie_export_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("portal_id", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("plan_version_id", sa.Integer(), nullable=False),
        sa.Column("export_job_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("error_row_count", sa.Integer(), nullable=False),
        sa.Column("result_summary_json", JSONType, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ie_export_runs_portal_id", "ie_export_runs", ["portal_id"], unique=False)
    op.create_index("ix_ie_export_runs_user_id", "ie_export_runs", ["user_id"], unique=False)
    op.create_index(
        "ix_ie_export_runs_plan_version_id",
        "ie_export_runs",
        ["plan_version_id"],
        unique=False,
    )

    op.add_column("export_jobs", sa.Column("created_by_user_id", sa.Integer(), nullable=True))
    op.add_column("export_jobs", sa.Column("plan_version_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("export_jobs", "plan_version_id")
    op.drop_column("export_jobs", "created_by_user_id")
    op.drop_table("ie_export_runs")
    op.drop_table("ie_memory_entries")
    op.drop_table("ie_export_plan_versions")
    op.drop_table("ie_messages")
    op.drop_table("ie_conversations")
    op.drop_table("app_users")
