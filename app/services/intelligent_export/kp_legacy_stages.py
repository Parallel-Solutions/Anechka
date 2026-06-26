"""Legacy funnel 15 stage IDs for pre-migration deals (category_id NULL)."""

from __future__ import annotations

from sqlalchemy.orm import Session

KP_CATEGORY_ID = 15

# Named + numeric stage codes from funnel «Коммерческое предложение» before C15:* migration.
LEGACY_KP_STAGE_IDS: frozenset[str] = frozenset(
    {
        "3",
        "4",
        "5",
        "7",
        "8",
        "9",
        "NEW",
        "PREPARATION",
        "PREPAYMENT_INVOICE",
        "EXECUTING",
        "FINAL_INVOICE",
    }
)


def _numeric_stage_ids(stages: list[dict]) -> frozenset[str]:
    out: set[str] = set()
    for stage in stages:
        code = str(stage.get("id") or stage.get("STATUS_ID") or "").strip()
        if code and ":" not in code:
            out.add(code)
    return frozenset(out)


def legacy_kp_stage_ids(db: Session, portal_id: str) -> frozenset[str]:
    """All legacy KP funnel stage codes: static fallback ∪ numeric stages from CRM dictionary."""
    from app.services.intelligent_export.tomoru_stages import _load_stages_from_db

    stages = _load_stages_from_db(db, portal_id, KP_CATEGORY_ID)
    numeric = _numeric_stage_ids(stages)
    if numeric:
        return LEGACY_KP_STAGE_IDS | numeric
    return LEGACY_KP_STAGE_IDS
