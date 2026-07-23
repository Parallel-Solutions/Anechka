"""Tests for the seven business-facing call-result groups."""

import pytest

from app.models import CallResultImportRow
from app.routers.call_results import _row_list_out
from app.services.call_results.business_groups import (
    BUSINESS_GROUP_LABELS,
    count_business_groups,
    get_business_group,
    get_business_group_code,
)


def _row(*, primary_outcome=None, signals=None, normalized_data=None):
    return CallResultImportRow(
        id=1,
        import_id=1,
        source_row_number=2,
        raw_data={},
        normalized_data=normalized_data or {},
        raw_phone="+79161234567",
        normalized_phone="9161234567",
        match_status="matched",
        llm_status="not_required",
        llm_required=False,
        manually_overridden=False,
        llm_input_truncated=False,
        is_duplicate=False,
        needs_manual_review=False,
        execution_status="prepared",
        matched_deal_id=1001,
        primary_outcome=primary_outcome,
        business_signals=signals or {},
    )


@pytest.mark.parametrize(
    ("primary_outcome", "signals", "normalized_data", "expected"),
    [
        ("positive", {"positive": True}, {}, "conversation_yes"),
        ("refusal", {"explicit_refusal": True}, {}, "conversation_no"),
        ("callback_later", {"callback_later_requested": True}, {}, "callback_same"),
        ("alternate_contact", {"alternate_contact_requested": True}, {}, "callback_other"),
        (
            "mixed",
            {"positive": True, "explicit_refusal": True},
            {},
            "conversation_unclear",
        ),
        ("no_answer", {"no_answer": True}, {}, "no_answer"),
        (None, {}, {}, "other"),
        (
            "manual_review",
            {},
            {"has_meaningful_content": True},
            "conversation_unclear",
        ),
    ],
)
def test_business_group_mapping(primary_outcome, signals, normalized_data, expected):
    row = _row(
        primary_outcome=primary_outcome,
        signals=signals,
        normalized_data=normalized_data,
    )

    code, label = get_business_group(row)

    assert code == expected
    assert label == BUSINESS_GROUP_LABELS[expected]


def test_business_group_catalog_contains_exactly_seven_groups():
    assert set(BUSINESS_GROUP_LABELS) == {
        "conversation_yes",
        "conversation_no",
        "callback_same",
        "callback_other",
        "conversation_unclear",
        "no_answer",
        "other",
    }


def test_business_group_counts_include_zero_values():
    counts = count_business_groups(
        [_row(primary_outcome="positive", signals={"positive": True})]
    )

    assert len(counts) == 7
    assert counts["conversation_yes"] == 1
    assert counts["conversation_no"] == 0

def test_row_api_projection_contains_business_group_and_label():
    row = _row(
        primary_outcome="refusal",
        signals={"explicit_refusal": True},
    )

    result = _row_list_out(row, "example.bitrix24.ru")

    assert result.business_group == "conversation_no"
    assert result.business_group_label == "РАЗГОВОР БЫЛ, НЕТ (отказ)"
    assert get_business_group_code(row) == result.business_group
