"""Tests for manual review queue eligibility."""

from app.models import CallResultImportRow
from app.services.call_results.manual_review_service import (
    get_available_manual_actions,
    is_pending_manual_review_row,
)


def _row(**kw) -> CallResultImportRow:
    defaults = dict(
        id=1,
        import_id=1,
        source_row_number=2,
        raw_data={},
        normalized_data={},
        match_status="matched",
        llm_status="not_required",
        llm_required=False,
        manually_overridden=False,
        llm_input_truncated=False,
        is_duplicate=False,
        needs_manual_review=False,
        execution_status="blocked_manual_review",
        matched_deal_id=1001,
        operator_filter=None,
        raw_phone="89161234567",
        normalized_phone="89161234567",
    )
    defaults.update(kw)
    return CallResultImportRow(**defaults)


def test_available_actions_with_deal_and_phone():
    row = _row()
    actions = get_available_manual_actions(row, contact_creation_allowed=True)
    assert actions == ["comment", "todo", "find_contact", "create_contact"]


def test_available_actions_without_deal():
    row = _row(matched_deal_id=None, match_status="not_found")
    actions = get_available_manual_actions(row, contact_creation_allowed=True)
    assert actions == ["create_contact"]


def test_available_actions_without_contact_creation():
    row = _row(matched_deal_id=None, match_status="not_found")
    actions = get_available_manual_actions(row, contact_creation_allowed=False)
    assert actions == []


def test_pending_requires_needs_manual_review_flag():
    row = _row(needs_manual_review=False)
    assert not is_pending_manual_review_row(row, contact_creation_allowed=True)


def test_pending_requires_blocked_status():
    row = _row(needs_manual_review=True, execution_status="prepared")
    assert not is_pending_manual_review_row(row, contact_creation_allowed=True)


def test_pending_excludes_operator_filter():
    row = _row(needs_manual_review=True, operator_filter="new_comments")
    assert not is_pending_manual_review_row(row, contact_creation_allowed=True)


def test_pending_with_deal_is_actionable():
    row = _row(needs_manual_review=True)
    assert is_pending_manual_review_row(row, contact_creation_allowed=True)


def test_pending_not_found_only_with_create_contact():
    row = _row(
        needs_manual_review=True,
        matched_deal_id=None,
        match_status="not_found",
    )
    assert is_pending_manual_review_row(row, contact_creation_allowed=True)
    assert not is_pending_manual_review_row(row, contact_creation_allowed=False)


def test_pending_not_found_without_phone_not_actionable():
    row = _row(
        needs_manual_review=True,
        matched_deal_id=None,
        match_status="not_found",
        raw_phone=None,
        normalized_phone=None,
    )
    assert not is_pending_manual_review_row(row, contact_creation_allowed=True)
