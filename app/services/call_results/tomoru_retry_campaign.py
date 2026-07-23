"""Build deterministic Tomoru campaign drafts from the local retry queue."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models import CallRetryQueueEntry

DEFAULT_TIMEZONE = "Europe/Moscow"


@dataclass(frozen=True)
class TomoruDraftContact:
    queue_entry_id: int
    phone_number: str
    phone_extension: str | None
    deal_id: int | None
    contact_id: int | None
    callback_text: str | None


@dataclass
class TomoruCampaignDraft:
    idempotency_key: str
    campaign_name: str
    timezone: str
    scheduled_at: datetime
    reason: str
    contacts: list[TomoruDraftContact] = field(default_factory=list)

    def as_dict(self) -> dict:
        result = asdict(self)
        result["scheduled_at"] = self.scheduled_at.isoformat()
        result["local_call_time"] = self.scheduled_at.strftime("%H:%M")
        result["contact_count"] = len(self.contacts)
        return result


class TomoruRetryCampaignPlanner:
    """Create dry-run campaign payloads; this class performs no HTTP calls."""

    def __init__(self, default_local_call_time: str = "10:00"):
        self.default_local_call_time = self._parse_local_time(default_local_call_time)

    def plan(
        self,
        entries: list[CallRetryQueueEntry],
        *,
        now: datetime | None = None,
    ) -> list[TomoruCampaignDraft]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        groups: dict[tuple[str, str, str], list[CallRetryQueueEntry]] = {}

        for entry in entries:
            if entry.status not in {"ready", "scheduled", "failed"}:
                continue
            if entry.search_required or not entry.phone_normalized:
                continue
            timezone_name, zone = self._zone(entry.timezone)
            scheduled_at = self._schedule(entry.callback_at, zone, current)
            schedule_key = scheduled_at.replace(second=0, microsecond=0).isoformat()
            key = (timezone_name, schedule_key, entry.reason)
            groups.setdefault(key, []).append(entry)

        drafts: list[TomoruCampaignDraft] = []
        for (timezone_name, schedule_key, reason), grouped in sorted(groups.items()):
            scheduled_at = datetime.fromisoformat(schedule_key)
            queue_ids = sorted(entry.id for entry in grouped)
            raw_key = f"{timezone_name}|{schedule_key}|{reason}|{','.join(map(str, queue_ids))}"
            idempotency_key = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
            draft = TomoruCampaignDraft(
                idempotency_key=idempotency_key,
                campaign_name=self._campaign_name(reason, timezone_name, scheduled_at),
                timezone=timezone_name,
                scheduled_at=scheduled_at,
                reason=reason,
                contacts=[
                    TomoruDraftContact(
                        queue_entry_id=entry.id,
                        phone_number=str(entry.phone_normalized),
                        phone_extension=entry.phone_extension,
                        deal_id=entry.deal_id,
                        contact_id=entry.contact_id,
                        callback_text=entry.callback_text,
                    )
                    for entry in sorted(grouped, key=lambda item: item.id)
                ],
            )
            drafts.append(draft)
        return drafts

    def _schedule(
        self,
        callback_at: datetime | None,
        zone: ZoneInfo,
        current: datetime,
    ) -> datetime:
        if callback_at is not None:
            if callback_at.tzinfo is None:
                return callback_at.replace(tzinfo=zone, second=0, microsecond=0)
            return callback_at.astimezone(zone).replace(second=0, microsecond=0)

        local_now = current.astimezone(zone)
        candidate = datetime.combine(local_now.date(), self.default_local_call_time, tzinfo=zone)
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate

    @staticmethod
    def _zone(timezone_name: str | None) -> tuple[str, ZoneInfo]:
        name = timezone_name or DEFAULT_TIMEZONE
        try:
            return name, ZoneInfo(name)
        except ZoneInfoNotFoundError:
            return DEFAULT_TIMEZONE, ZoneInfo(DEFAULT_TIMEZONE)

    @staticmethod
    def _parse_local_time(value: str) -> time:
        try:
            hour_text, minute_text = value.strip().split(":", 1)
            return time(hour=int(hour_text), minute=int(minute_text))
        except (AttributeError, TypeError, ValueError):
            return time(hour=10, minute=0)

    @staticmethod
    def _campaign_name(reason: str, timezone_name: str, scheduled_at: datetime) -> str:
        safe_reason = re.sub(r"[^a-zA-Z0-9_-]+", "_", reason).strip("_") or "retry"
        safe_timezone = timezone_name.replace("/", "_")
        timestamp = scheduled_at.strftime("%Y%m%d_%H%M")
        return f"Anechka_{safe_reason}_{safe_timezone}_{timestamp}"
