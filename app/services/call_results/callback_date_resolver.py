"""Resolve callback dates from text."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

AMBIGUOUS_PATTERNS = [
    r"осен", r"зим", r"весн", r"лет", r"после\s+праздник", r"следующ",
    r"после\s+обед", r"позже", r"не\s+скоро",
]


@dataclass
class ResolvedDeadline:
    callback_at: datetime | None
    callback_text: str | None
    is_ambiguous: bool = False
    warning: str | None = None


class CallbackDateResolver:
    def resolve(
        self,
        callback_text: str | None,
        callback_at_hint: datetime | None,
        called_at: datetime | None,
        *,
        timezone: str = "Europe/Moscow",
    ) -> ResolvedDeadline:
        if callback_at_hint and callback_text is None:
            return ResolvedDeadline(callback_at=callback_at_hint, callback_text=None)

        if not callback_text and not callback_at_hint:
            return ResolvedDeadline(callback_at=None, callback_text=None)

        text = (callback_text or "").strip().lower()
        if not text:
            return ResolvedDeadline(callback_at=callback_at_hint, callback_text=callback_text)

        for pat in AMBIGUOUS_PATTERNS:
            if re.search(pat, text):
                return ResolvedDeadline(
                    callback_at=None,
                    callback_text=callback_text,
                    is_ambiguous=True,
                    warning="Неоднозначный срок",
                )

        if "после" in text and re.search(r"\d{1,2}\s+\w+", text):
            return ResolvedDeadline(
                callback_at=None,
                callback_text=callback_text,
                is_ambiguous=True,
                warning="Относительная дата без точного времени",
            )

        if called_at is None:
            return ResolvedDeadline(
                callback_at=None,
                callback_text=callback_text,
                is_ambiguous=True,
                warning="called_at отсутствует для относительного срока",
            )

        tz = ZoneInfo(timezone)
        base = called_at
        if base.tzinfo is None:
            base = base.replace(tzinfo=tz)
        else:
            base = base.astimezone(tz)

        if "завтра" in text:
            dt = base + timedelta(days=1)
            m = re.search(r"(\d{1,2})[:\.](\d{2})", text)
            if m:
                dt = dt.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
            elif "половин" in text and "десят" in text:
                dt = dt.replace(hour=9, minute=30, second=0, microsecond=0)
            return ResolvedDeadline(callback_at=dt, callback_text=callback_text)

        m = re.search(r"через\s+(\d+)\s+час", text)
        if m:
            dt = base + timedelta(hours=int(m.group(1)))
            return ResolvedDeadline(callback_at=dt, callback_text=callback_text)

        m = re.search(r"через\s+(\d+)\s+д", text)
        if m:
            dt = base + timedelta(days=int(m.group(1)))
            return ResolvedDeadline(callback_at=dt, callback_text=callback_text)

        return ResolvedDeadline(
            callback_at=None,
            callback_text=callback_text,
            is_ambiguous=True,
            warning="Не удалось определить точный срок",
        )
