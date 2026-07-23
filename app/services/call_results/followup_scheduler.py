"""Schedule delayed follow-ups for call-result outcomes."""

from __future__ import annotations

import calendar
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Europe/Moscow"


def _timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TIMEZONE)


def _add_calendar_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def schedule_refusal_followup(
    called_at: datetime | None,
    timezone_name: str | None,
    *,
    now: datetime | None = None,
) -> datetime:
    """Return a timezone-aware deadline exactly three calendar months later."""

    timezone = _timezone(timezone_name)
    if called_at is None:
        base = now or datetime.now(timezone)
        if base.tzinfo is None:
            base = base.replace(tzinfo=timezone)
        else:
            base = base.astimezone(timezone)
        base = base.replace(hour=10, minute=0, second=0, microsecond=0)
    elif called_at.tzinfo is None:
        base = called_at.replace(tzinfo=timezone)
    else:
        base = called_at.astimezone(timezone)
    return _add_calendar_months(base, 3)