"""call result imports and bitrix prepared actions

Revision ID: 8f6b3c2d1e45
Revises: 7e5a2b9c1d34
Create Date: 2026-06-29 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

revision: str = "8f6b3c2d1e45"
down_revision: Union[str, None] = "7e5a2b9c1d34"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "call_result_imports",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("portal_id", sa.String(length=255), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("storage_key", sa.String(length=1024), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("file_size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("selected_sheet", sa.String(length=255), nullable=True),
        sa.Column("column_mapping", JSONType, nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="uploaded"),
        sa.Column("total_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("matched_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("review_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("parser_version", sa.String(length=16), nullable=False, server_default="1"),
        sa.Column("planner_version", sa.String(length=16), nullable=False, server_default="1"),
        sa.Column("classifier_version", sa.String(length=16), nullable=False, server_default="1"),
        sa.Column("duplicate_of_import_id", sa.Integer(), nullable=True),
        sa.Column("llm_rows_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_rows_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_rows_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_rows_cached", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_rows_skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_rows_low_confidence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_estimated_cost_usd", sa.Float(), nullable=True),
        sa.Column("deterministic_classified", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_call_result_imports_portal_id", "call_result_imports", ["portal_id"])
    op.create_index("ix_call_result_imports_file_sha256", "call_result_imports", ["file_sha256"])
    op.create_index("ix_call_result_imports_status", "call_result_imports", ["status"])

    op.create_table(
        "call_result_import_rows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("import_id", sa.Integer(), nullable=False),
        sa.Column("source_row_number", sa.Integer(), nullable=False),
        sa.Column("raw_data", JSONType, nullable=False),
        sa.Column("normalized_data", JSONType, nullable=False),
        sa.Column("raw_phone", sa.String(length=128), nullable=True),
        sa.Column("normalized_phone", sa.String(length=32), nullable=True),
        sa.Column("phone_extension", sa.String(length=32), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("call_id", sa.String(length=128), nullable=True),
        sa.Column("campaign_id", sa.String(length=128), nullable=True),
        sa.Column("called_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("callback_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("matched_contact_id", sa.BigInteger(), nullable=True),
        sa.Column("matched_deal_id", sa.BigInteger(), nullable=True),
        sa.Column("matched_company_id", sa.BigInteger(), nullable=True),
        sa.Column("matched_deal_local_id", sa.Integer(), nullable=True),
        sa.Column("match_status", sa.String(length=32), nullable=False, server_default="not_found"),
        sa.Column("match_reason", sa.Text(), nullable=True),
        sa.Column("candidate_matches", JSONType, nullable=True),
        sa.Column("processing_errors", JSONType, nullable=True),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("is_duplicate", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("row_hash", sa.String(length=64), nullable=True),
        sa.Column("deterministic_category", sa.String(length=32), nullable=True),
        sa.Column("deterministic_reason", sa.Text(), nullable=True),
        sa.Column("llm_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("llm_status", sa.String(length=32), nullable=False, server_default="not_required"),
        sa.Column("llm_provider", sa.String(length=32), nullable=True),
        sa.Column("llm_model", sa.String(length=64), nullable=True),
        sa.Column("llm_prompt_version", sa.String(length=16), nullable=True),
        sa.Column("llm_schema_version", sa.String(length=16), nullable=True),
        sa.Column("llm_input_hash", sa.String(length=64), nullable=True),
        sa.Column("llm_input_truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("llm_result", JSONType, nullable=True),
        sa.Column("llm_confidence", sa.Float(), nullable=True),
        sa.Column("llm_validation_errors", JSONType, nullable=True),
        sa.Column("llm_error_type", sa.String(length=64), nullable=True),
        sa.Column("llm_duration_ms", sa.Integer(), nullable=True),
        sa.Column("llm_token_usage", JSONType, nullable=True),
        sa.Column("final_category", sa.String(length=32), nullable=True),
        sa.Column("classification_source", sa.String(length=16), nullable=True),
        sa.Column("classification_reason", sa.Text(), nullable=True),
        sa.Column("extracted_data", JSONType, nullable=True),
        sa.Column("manually_overridden", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("manually_overridden_by", sa.String(length=255), nullable=True),
        sa.Column("manually_overridden_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["import_id"], ["call_result_imports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_call_result_import_rows_import_id", "call_result_import_rows", ["import_id"])

    op.create_table(
        "bitrix_prepared_actions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("import_id", sa.Integer(), nullable=False),
        sa.Column("import_row_id", sa.Integer(), nullable=False),
        sa.Column("action_group_id", sa.String(length=36), nullable=False),
        sa.Column("method", sa.String(length=64), nullable=False),
        sa.Column("action_type", sa.String(length=32), nullable=False),
        sa.Column("payload", JSONType, nullable=False),
        sa.Column("human_summary", sa.Text(), nullable=True),
        sa.Column("validation_status", sa.String(length=16), nullable=False, server_default="valid"),
        sa.Column("validation_errors", JSONType, nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("user_modified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("modified_fields", JSONType, nullable=True),
        sa.Column("idempotency_key", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["import_id"], ["call_result_imports.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["import_row_id"], ["call_result_import_rows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bitrix_prepared_actions_import_id", "bitrix_prepared_actions", ["import_id"])
    op.create_index("ix_bitrix_prepared_actions_import_row_id", "bitrix_prepared_actions", ["import_row_id"])

    op.create_table(
        "call_result_llm_cache",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("portal_id", sa.String(length=255), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=16), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("result_json", JSONType, nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("token_usage", JSONType, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("portal_id", "input_hash", name="uq_call_result_llm_cache"),
    )
    op.create_index(
        "ix_call_result_llm_cache_portal_hash",
        "call_result_llm_cache",
        ["portal_id", "input_hash"],
    )


def downgrade() -> None:
    op.drop_table("call_result_llm_cache")
    op.drop_table("bitrix_prepared_actions")
    op.drop_table("call_result_import_rows")
    op.drop_table("call_result_imports")
