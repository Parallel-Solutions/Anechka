"""Tomoru call result fields and audit table

Revision ID: 9a7c4e5f2b01
Revises: 8f6b3c2d1e45
Create Date: 2026-06-30 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

revision: str = "9a7c4e5f2b01"
down_revision: Union[str, None] = "8f6b3c2d1e45"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")


def upgrade() -> None:
    op.add_column("call_result_imports", sa.Column("source_format", sa.String(length=32), nullable=True))
    op.add_column("call_result_imports", sa.Column("batch_id", sa.String(length=64), nullable=True))
    op.add_column("call_result_imports", sa.Column("exported_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "call_result_imports",
        sa.Column("adapter_version", sa.String(length=16), nullable=False, server_default="1"),
    )
    op.add_column("call_result_imports", sa.Column("import_warnings", JSONType, nullable=True))
    op.create_index("ix_call_result_imports_source_format", "call_result_imports", ["source_format"])
    op.create_index("ix_call_result_imports_batch_id", "call_result_imports", ["batch_id"])

    op.add_column("call_result_import_rows", sa.Column("source_identity", sa.String(length=128), nullable=True))
    op.add_column("call_result_import_rows", sa.Column("technical_status", sa.String(length=64), nullable=True))
    op.add_column("call_result_import_rows", sa.Column("call_result_display", sa.String(length=64), nullable=True))
    op.add_column("call_result_import_rows", sa.Column("attempts", sa.Integer(), nullable=True))
    op.add_column("call_result_import_rows", sa.Column("processing_warnings", JSONType, nullable=True))
    op.add_column("call_result_import_rows", sa.Column("llm_category", sa.String(length=32), nullable=True))
    op.add_column("call_result_import_rows", sa.Column("merge_conflict_reason", sa.Text(), nullable=True))
    op.create_index("ix_call_result_import_rows_source_identity", "call_result_import_rows", ["source_identity"])
    op.create_index("ix_call_result_import_rows_call_result_display", "call_result_import_rows", ["call_result_display"])
    op.create_index("ix_call_result_import_rows_normalized_phone", "call_result_import_rows", ["normalized_phone"])

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("bitrix_prepared_actions") as batch_op:
            batch_op.create_unique_constraint(
                "uq_bitrix_prepared_actions_idempotency",
                ["import_id", "idempotency_key"],
            )
    else:
        op.create_unique_constraint(
            "uq_bitrix_prepared_actions_idempotency",
            "bitrix_prepared_actions",
            ["import_id", "idempotency_key"],
        )

    op.create_table(
        "call_result_row_audit",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("row_id", sa.Integer(), nullable=False),
        sa.Column("changed_by", sa.String(length=255), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("field_name", sa.String(length=64), nullable=False),
        sa.Column("old_value", JSONType, nullable=True),
        sa.Column("new_value", JSONType, nullable=True),
        sa.ForeignKeyConstraint(["row_id"], ["call_result_import_rows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_call_result_row_audit_row_id", "call_result_row_audit", ["row_id"])


def downgrade() -> None:
    op.drop_index("ix_call_result_row_audit_row_id", table_name="call_result_row_audit")
    op.drop_table("call_result_row_audit")

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("bitrix_prepared_actions") as batch_op:
            batch_op.drop_constraint("uq_bitrix_prepared_actions_idempotency", type_="unique")
    else:
        op.drop_constraint("uq_bitrix_prepared_actions_idempotency", "bitrix_prepared_actions", type_="unique")

    op.drop_index("ix_call_result_import_rows_normalized_phone", table_name="call_result_import_rows")
    op.drop_index("ix_call_result_import_rows_call_result_display", table_name="call_result_import_rows")
    op.drop_index("ix_call_result_import_rows_source_identity", table_name="call_result_import_rows")
    op.drop_column("call_result_import_rows", "merge_conflict_reason")
    op.drop_column("call_result_import_rows", "llm_category")
    op.drop_column("call_result_import_rows", "processing_warnings")
    op.drop_column("call_result_import_rows", "attempts")
    op.drop_column("call_result_import_rows", "call_result_display")
    op.drop_column("call_result_import_rows", "technical_status")
    op.drop_column("call_result_import_rows", "source_identity")

    op.drop_index("ix_call_result_imports_batch_id", table_name="call_result_imports")
    op.drop_index("ix_call_result_imports_source_format", table_name="call_result_imports")
    op.drop_column("call_result_imports", "import_warnings")
    op.drop_column("call_result_imports", "adapter_version")
    op.drop_column("call_result_imports", "exported_at")
    op.drop_column("call_result_imports", "batch_id")
    op.drop_column("call_result_imports", "source_format")
