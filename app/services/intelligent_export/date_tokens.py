"""Resolve relative date tokens emitted by the planner into absolute dates.

The AI is instructed to never invent absolute dates from the user's relative
phrasing ("за последний месяц"). Instead it emits stable tokens that the server
resolves deterministically against a known "today". This keeps date handling
testable and timezone-correct on the server side.

Supported tokens (string filter values):
  @today, @today-Nd, @today+Nd, @today-Nm, @month_start, @month_end,
  @prev_month_start, @prev_month_end, @year_start, @year_end
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Any

_OFFSET_RE = re.compile(r"^@today([+-]\d+)([dm])$")


def _last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def resolve_token(token: str, today: date) -> str | None:
    if not isinstance(token, str) or not token.startswith("@"):
        return None
    t = token.strip().lower()
    if t == "@today":
        return today.isoformat()
    if t == "@month_start":
        return today.replace(day=1).isoformat()
    if t == "@month_end":
        return today.replace(day=_last_day(today.year, today.month)).isoformat()
    if t == "@prev_month_end":
        first = today.replace(day=1)
        prev = first - timedelta(days=1)
        return prev.isoformat()
    if t == "@prev_month_start":
        first = today.replace(day=1)
        prev = first - timedelta(days=1)
        return prev.replace(day=1).isoformat()
    if t == "@year_start":
        return today.replace(month=1, day=1).isoformat()
    if t == "@year_end":
        return today.replace(month=12, day=31).isoformat()
    m = _OFFSET_RE.match(t)
    if m:
        amount = int(m.group(1))
        unit = m.group(2)
        if unit == "d":
            return (today + timedelta(days=amount)).isoformat()
        if unit == "m":
            month_index = (today.year * 12 + (today.month - 1)) + amount
            year = month_index // 12
            month = month_index % 12 + 1
            day = min(today.day, _last_day(year, month))
            return date(year, month, day).isoformat()
    return None


def _resolve_value(value: Any, today: date) -> Any:
    if isinstance(value, str):
        resolved = resolve_token(value, today)
        return resolved if resolved is not None else value
    if isinstance(value, list):
        return [_resolve_value(v, today) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_value(v, today) for k, v in value.items()}
    return value


def resolve_date_tokens(plan_dict: dict, today: date) -> dict:
    """Walk the plan and replace any @-tokens in filter/condition values."""
    return _resolve_value(plan_dict, today)
