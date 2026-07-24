"""Plan initial Tomoru campaigns grouped by the recipient timezone."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TIMEZONE = "Europe/Moscow"


@dataclass(frozen=True)
class TomoruInitialBatchDraft:
    campaign_name: str
    timezone: str
    scheduled_at: datetime
    rows: tuple[Any, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "campaign_name": self.campaign_name,
            "timezone": self.timezone,
            "scheduled_at": self.scheduled_at.isoformat(),
            "contact_count": len(self.rows),
        }


class TomoruInitialCampaignPlanner:
    """Create one campaign per timezone for the next requested local time."""

    def plan(
        self,
        rows: Iterable[Any],
        *,
        local_call_time: str = "10:00",
        now: datetime | None = None,
    ) -> list[TomoruInitialBatchDraft]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        call_time = self._parse_time(local_call_time)

        grouped: dict[str, list[Any]] = defaultdict(list)
        seen: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            timezone_name, _ = self._zone(getattr(row, "timezone", None))
            phone = str(getattr(row, "phone", "") or "")
            if not phone or phone in seen[timezone_name]:
                continue
            seen[timezone_name].add(phone)
            grouped[timezone_name].append(row)

        drafts: list[TomoruInitialBatchDraft] = []
        for timezone_name in sorted(grouped):
            _, zone = self._zone(timezone_name)
            local_now = current.astimezone(zone)
            scheduled_local = datetime.combine(local_now.date(), call_time, tzinfo=zone)
            if scheduled_local <= local_now + timedelta(minutes=2):
                scheduled_local += timedelta(days=1)
            scheduled_at = scheduled_local.astimezone(timezone.utc)
            safe_timezone = timezone_name.replace("/", "_")
            timestamp = scheduled_local.strftime("%Y%m%d_%H%M")
            drafts.append(
                TomoruInitialBatchDraft(
                    campaign_name=f"Анечка_{safe_timezone}_{timestamp}",
                    timezone=timezone_name,
                    scheduled_at=scheduled_at,
                    rows=tuple(grouped[timezone_name]),
                )
            )
        return drafts

    @staticmethod
    def _parse_time(value: str) -> time:
        try:
            hour, minute = (int(part) for part in value.split(":", 1))
            return time(hour=hour, minute=minute)
        except (TypeError, ValueError) as exc:
            raise ValueError("Некорректное местное время запуска") from exc

    @staticmethod
    def _zone(value: str | None) -> tuple[str, ZoneInfo]:
        name = str(value or DEFAULT_TIMEZONE)
        try:
            return name, ZoneInfo(name)
        except ZoneInfoNotFoundError:
            return DEFAULT_TIMEZONE, ZoneInfo(DEFAULT_TIMEZONE)