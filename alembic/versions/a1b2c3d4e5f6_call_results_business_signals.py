"""Call results business signals, retry queue, contact search, execution fields

Revision ID: a1b2c3d4e5f6
Revises: 9a7c4e5f2b01
Create Date: 2026-06-30 18:00:00.000000

"""
from typing import Sequence, Union

import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy import Text
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "9a7c4e5f2b01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")


def _migrate_row_signals(connection) -> None:
    rows = connection.execute(
        sa.text(
            "SELECT id, final_category, extracted_data, llm_result, technical_status, call_result_display "
            "FROM call_result_import_rows"
        )
    ).fetchall()
    for row in rows:
        rid, cat, extracted, llm_result, tech, display = row
        ext = json.loads(extracted) if isinstance(extracted, str) else (extracted or {})
        llm = json.loads(llm_result) if isinstance(llm_result, str) else (llm_result or {})
        signals = {
            "positive": False,
            "alternate_contact_requested": False,
            "callback_later_requested": False,
            "explicit_refusal": False,
            "hangup_without_result": False,
            "alternate_contact": {
                "name": ext.get("contact_name") or llm.get("contact_name"),
                "phone": ext.get("full_phone") or llm.get("full_phone"),
                "extension": ext.get("phone_extension") or llm.get("phone_extension"),
                "email": ext.get("email") or llm.get("email"),
                "position": ext.get("contact_role") or llm.get("contact_role"),
            },
            "callback_at": None,
            "callback_text": ext.get("callback_text") or llm.get("callback_text"),
            "summary": ext.get("summary") or llm.get("summary") or "",
            "refusal_reason": None,
            "confidence": llm.get("confidence", 0.0) if llm else 0.0,
            "needs_manual_review": False,
            "manual_review_reason": None,
        }
        primary = "manual_review"
        tech_l = (tech or display or "").lower()
        if cat == "hot_lead":
            signals["positive"] = True
            primary = "positive"
        elif cat == "refusal":
            signals["explicit_refusal"] = True
            primary = "refusal"
        elif cat == "manager_callback":
            if signals["alternate_contact"]["phone"] or signals["alternate_contact"]["name"]:
                signals["alternate_contact_requested"] = True
                primary = "alternate_contact"
            elif signals["callback_text"]:
                signals["callback_later_requested"] = True
                primary = "callback_later"
            else:
                signals["needs_manual_review"] = True
                signals["manual_review_reason"] = "Legacy manager_callback без данных"
                primary = "manual_review"
        elif cat == "robot_callback":
            if any(x in tech_l for x in ("no answer", "voicemail", "busy", "noanswer")):
                signals["needs_manual_review"] = True
                signals["manual_review_reason"] = "Legacy technical outcome без утверждённого действия"
                primary = "unsupported_outcome"
            else:
                signals["needs_manual_review"] = True
                signals["manual_review_reason"] = "Legacy robot_callback требует ручной проверки"
                primary = "manual_review"
        elif cat == "unknown":
            signals["needs_manual_review"] = True
            primary = "manual_review"
        connection.execute(
            sa.text(
                "UPDATE call_result_import_rows SET business_signals = :sig, primary_outcome = :po, "
                "needs_manual_review = :nmr, manual_review_reason = :mrr WHERE id = :id"
            ),
            {
                "sig": json.dumps(signals, ensure_ascii=False),
                "po": primary,
                "nmr": signals["needs_manual_review"],
                "mrr": signals["manual_review_reason"],
                "id": rid,
            },
        )


def upgrade() -> None:
    # import execute tracking
    op.add_column("call_result_imports", sa.Column("execute_status", sa.String(length=32), nullable=True))
    op.add_column(
        "call_result_imports",
        sa.Column("execute_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "call_result_imports",
        sa.Column("execute_completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # row signals
    op.add_column("call_result_import_rows", sa.Column("business_signals", JSONType, nullable=True))
    op.add_column("call_result_import_rows", sa.Column("primary_outcome", sa.String(length=32), nullable=True))
    op.add_column(
        "call_result_import_rows",
        sa.Column("needs_manual_review", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("call_result_import_rows", sa.Column("manual_review_reason", sa.Text(), nullable=True))
    op.add_column("call_result_import_rows", sa.Column("row_classifier_version", sa.String(length=16), nullable=True))
    op.add_column("call_result_import_rows", sa.Column("row_planner_version", sa.String(length=16), nullable=True))
    op.add_column(
        "call_result_import_rows",
        sa.Column("execution_status", sa.String(length=32), nullable=False, server_default="pending"),
    )
    op.create_index("ix_crir_primary_outcome", "call_result_import_rows", ["primary_outcome"])
    op.create_index("ix_crir_execution_status", "call_result_import_rows", ["execution_status"])

    # prepared action execution
    op.add_column("bitrix_prepared_actions", sa.Column("operation_type", sa.String(length=32), nullable=True))
    op.add_column(
        "bitrix_prepared_actions",
        sa.Column("execution_status", sa.String(length=32), nullable=False, server_default="prepared"),
    )
    op.add_column("bitrix_prepared_actions", sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("bitrix_prepared_actions", sa.Column("external_id", sa.String(length=128), nullable=True))
    op.add_column("bitrix_prepared_actions", sa.Column("request_payload", JSONType, nullable=True))
    op.add_column("bitrix_prepared_actions", sa.Column("response_payload", JSONType, nullable=True))
    op.add_column("bitrix_prepared_actions", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column("bitrix_prepared_actions", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bitrix_prepared_actions", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bitrix_prepared_actions", sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_bpa_execution_status", "bitrix_prepared_actions", ["execution_status"])
    op.create_index(
        "ix_bpa_row_op_idem",
        "bitrix_prepared_actions",
        ["import_row_id", "operation_type", "idempotency_key"],
    )

    # disable legacy task actions
    op.execute(
        sa.text(
            "UPDATE bitrix_prepared_actions SET is_enabled = false, execution_status = 'skipped' "
            "WHERE method = 'tasks.task.add'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE bitrix_prepared_actions SET operation_type = action_type "
            "WHERE operation_type IS NULL"
        )
    )

    # retry queue
    op.create_table(
        "call_retry_queue_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("portal_id", sa.String(length=255), nullable=False),
        sa.Column("import_id", sa.Integer(), nullable=True),
        sa.Column("row_id", sa.Integer(), nullable=True),
        sa.Column("campaign_id", sa.String(length=128), nullable=True),
        sa.Column("source_call_id", sa.String(length=128), nullable=True),
        sa.Column("deal_id", sa.BigInteger(), nullable=True),
        sa.Column("contact_id", sa.BigInteger(), nullable=True),
        sa.Column("phone_normalized", sa.String(length=32), nullable=True),
        sa.Column("callback_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("callback_text", sa.Text(), nullable=True),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_contact_id", sa.BigInteger(), nullable=True),
        sa.Column("replacement_contact_id", sa.BigInteger(), nullable=True),
        sa.Column("search_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("idempotency_key", sa.String(length=512), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("portal_id", "idempotency_key", name="uq_call_retry_queue_idempotency"),
    )
    op.create_index("ix_crqe_portal_id", "call_retry_queue_entries", ["portal_id"])
    op.create_index("ix_crqe_import_id", "call_retry_queue_entries", ["import_id"])
    op.create_index("ix_crqe_row_id", "call_retry_queue_entries", ["row_id"])
    op.create_index("ix_crqe_deal_id", "call_retry_queue_entries", ["deal_id"])
    op.create_index("ix_crqe_phone", "call_retry_queue_entries", ["phone_normalized"])
    op.create_index("ix_crqe_callback_at", "call_retry_queue_entries", ["callback_at"])
    op.create_index("ix_crqe_status", "call_retry_queue_entries", ["status"])

    # contact search
    op.create_table(
        "call_contact_search_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("portal_id", sa.String(length=255), nullable=False),
        sa.Column("import_id", sa.Integer(), nullable=True),
        sa.Column("row_id", sa.Integer(), nullable=True),
        sa.Column("deal_id", sa.BigInteger(), nullable=True),
        sa.Column("company_id", sa.BigInteger(), nullable=True),
        sa.Column("region", sa.String(length=255), nullable=True),
        sa.Column("source_phone", sa.String(length=32), nullable=True),
        sa.Column("source_contact_id", sa.BigInteger(), nullable=True),
        sa.Column("deal_contact_ids", JSONType, nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("call_id", sa.String(length=128), nullable=True),
        sa.Column("campaign_id", sa.String(length=128), nullable=True),
        sa.Column("previous_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="contact_search_required"),
        sa.Column("found_contact_id", sa.BigInteger(), nullable=True),
        sa.Column("found_contact_phone", sa.String(length=32), nullable=True),
        sa.Column("confirmed_by", sa.String(length=255), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ccse_portal_id", "call_contact_search_entries", ["portal_id"])
    op.create_index("ix_ccse_import_id", "call_contact_search_entries", ["import_id"])
    op.create_index("ix_ccse_row_id", "call_contact_search_entries", ["row_id"])
    op.create_index("ix_ccse_deal_id", "call_contact_search_entries", ["deal_id"])
    op.create_index("ix_ccse_status", "call_contact_search_entries", ["status"])

    conn = op.get_bind()
    _migrate_row_signals(conn)


def downgrade() -> None:
    op.drop_index("ix_ccse_status", table_name="call_contact_search_entries")
    op.drop_index("ix_ccse_deal_id", table_name="call_contact_search_entries")
    op.drop_index("ix_ccse_row_id", table_name="call_contact_search_entries")
    op.drop_index("ix_ccse_import_id", table_name="call_contact_search_entries")
    op.drop_index("ix_ccse_portal_id", table_name="call_contact_search_entries")
    op.drop_table("call_contact_search_entries")

    op.drop_index("ix_crqe_status", table_name="call_retry_queue_entries")
    op.drop_index("ix_crqe_callback_at", table_name="call_retry_queue_entries")
    op.drop_index("ix_crqe_phone", table_name="call_retry_queue_entries")
    op.drop_index("ix_crqe_deal_id", table_name="call_retry_queue_entries")
    op.drop_index("ix_crqe_row_id", table_name="call_retry_queue_entries")
    op.drop_index("ix_crqe_import_id", table_name="call_retry_queue_entries")
    op.drop_index("ix_crqe_portal_id", table_name="call_retry_queue_entries")
    op.drop_table("call_retry_queue_entries")

    op.drop_index("ix_bpa_row_op_idem", table_name="bitrix_prepared_actions")
    op.drop_index("ix_bpa_execution_status", table_name="bitrix_prepared_actions")
    op.drop_column("bitrix_prepared_actions", "sort_order")
    op.drop_column("bitrix_prepared_actions", "completed_at")
    op.drop_column("bitrix_prepared_actions", "started_at")
    op.drop_column("bitrix_prepared_actions", "last_error")
    op.drop_column("bitrix_prepared_actions", "response_payload")
    op.drop_column("bitrix_prepared_actions", "request_payload")
    op.drop_column("bitrix_prepared_actions", "external_id")
    op.drop_column("bitrix_prepared_actions", "attempt_count")
    op.drop_column("bitrix_prepared_actions", "execution_status")
    op.drop_column("bitrix_prepared_actions", "operation_type")

    op.drop_index("ix_crir_execution_status", table_name="call_result_import_rows")
    op.drop_index("ix_crir_primary_outcome", table_name="call_result_import_rows")
    op.drop_column("call_result_import_rows", "execution_status")
    op.drop_column("call_result_import_rows", "row_planner_version")
    op.drop_column("call_result_import_rows", "row_classifier_version")
    op.drop_column("call_result_import_rows", "manual_review_reason")
    op.drop_column("call_result_import_rows", "needs_manual_review")
    op.drop_column("call_result_import_rows", "primary_outcome")
    op.drop_column("call_result_import_rows", "business_signals")

    op.drop_column("call_result_imports", "execute_completed_at")
    op.drop_column("call_result_imports", "execute_started_at")
    op.drop_column("call_result_imports", "execute_status")
