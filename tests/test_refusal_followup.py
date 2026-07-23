"""Tests for delayed follow-ups after an explicit refusal."""

from datetime import datetime, timezone

from app.services.call_results.followup_scheduler import schedule_refusal_followup


def test_refusal_followup_adds_three_calendar_months_and_clamps_day():
    result = schedule_refusal_followup(
        datetime(2026, 1, 31, 15, 45),
        "Europe/Moscow",
    )

    assert result.isoformat() == "2026-04-30T15:45:00+03:00"


def test_refusal_followup_rolls_over_year():
    result = schedule_refusal_followup(
        datetime(2026, 11, 30, 9, 5),
        "Europe/Moscow",
    )

    assert result.isoformat() == "2027-02-28T09:05:00+03:00"


def test_refusal_followup_converts_aware_call_time_to_deal_timezone():
    result = schedule_refusal_followup(
        datetime(2026, 2, 10, 7, 30, tzinfo=timezone.utc),
        "Europe/Moscow",
    )

    assert result.isoformat() == "2026-05-10T10:30:00+03:00"


def test_refusal_followup_without_call_time_uses_ten_am_local_time():
    result = schedule_refusal_followup(
        None,
        "Europe/Moscow",
        now=datetime(2026, 4, 21, 18, 12, tzinfo=timezone.utc),
    )

    assert result.isoformat() == "2026-07-21T10:00:00+03:00"


def test_refusal_followup_falls_back_to_moscow_for_unknown_timezone():
    result = schedule_refusal_followup(
        datetime(2026, 3, 15, 12, 0),
        "Invalid/Timezone",
    )

    assert result.isoformat() == "2026-06-15T12:00:00+03:00"