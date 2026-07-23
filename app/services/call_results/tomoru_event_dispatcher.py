"""Safe Tomoru event preparation and guarded external dispatch."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from app.config import Settings
from app.models import CallRetryQueueEntry, utcnow
from app.services.call_results.tomoru_retry_campaign import TomoruRetryCampaignPlanner


@dataclass(frozen=True)
class TomoruPreparedEvent:
    queue_entry_id: int
    scheduled_at: datetime
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "queue_entry_id": self.queue_entry_id,
            "scheduled_at": self.scheduled_at.isoformat(),
            "payload": self.payload,
        }


class TomoruCallbackUrlError(ValueError):
    pass


def validate_tomoru_callback_url(url: str, allowed_hosts: str) -> str:
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host:
        raise TomoruCallbackUrlError("Tomoru callback URL must use HTTPS")
    allowed = [item.strip().lower().rstrip(".") for item in allowed_hosts.split(",") if item.strip()]
    if not any(host == item or host.endswith("." + item) for item in allowed):
        raise TomoruCallbackUrlError("Tomoru callback host is not allowed")
    return parsed.geturl()


class TomoruEventDispatcher:
    """Prepare cold-call events and send them only behind an explicit feature flag."""

    def __init__(
        self,
        settings: Settings,
        *,
        post: Callable[..., Any] | None = None,
    ):
        self.settings = settings
        self._post = post or requests.post
        self._planner = TomoruRetryCampaignPlanner(settings.tomoru_default_local_call_time)

    def prepare(
        self,
        entries: list[CallRetryQueueEntry],
        *,
        now: datetime | None = None,
        include_future: bool = True,
    ) -> tuple[list[TomoruPreparedEvent], int]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        by_id = {entry.id: entry for entry in entries if entry.id is not None}
        prepared: list[TomoruPreparedEvent] = []
        future_count = 0

        for draft in self._planner.plan(entries, now=current):
            if draft.scheduled_at.astimezone(timezone.utc) > current.astimezone(timezone.utc):
                if not include_future:
                    future_count += len(draft.contacts)
                    continue
            for contact in draft.contacts:
                entry = by_id.get(contact.queue_entry_id)
                if entry is None:
                    continue
                payload = {
                    "event": self.settings.tomoru_event_name or "coldCall",
                    "trackingId": entry.idempotency_key,
                    "botId": self.settings.tomoru_bot_id or "TOMORU_BOT_ID_NOT_CONFIGURED",
                    "chatUri": self._chat_uri(contact.phone_number),
                    "data": {
                        "queueEntryId": entry.id,
                        "reason": entry.reason,
                        "scheduledAt": draft.scheduled_at.isoformat(),
                        "timezone": draft.timezone,
                        "phoneExtension": contact.phone_extension,
                        "dealId": contact.deal_id,
                        "contactId": contact.contact_id,
                        "callbackText": contact.callback_text,
                        "attemptCount": entry.attempt_count,
                    },
                }
                prepared.append(
                    TomoruPreparedEvent(
                        queue_entry_id=entry.id,
                        scheduled_at=draft.scheduled_at,
                        payload=payload,
                    )
                )
        return prepared, future_count

    def dispatch(
        self,
        entries: list[CallRetryQueueEntry],
        *,
        callback_url: str | None,
        dry_run: bool = True,
        include_future: bool = True,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        prepared, future_count = self.prepare(
            entries,
            now=current,
            include_future=include_future if dry_run else False,
        )
        report: dict[str, Any] = {
            "mode": "dry_run",
            "external_calls": False,
            "prepared": len(prepared),
            "sent": 0,
            "failed": 0,
            "skipped_future": future_count,
            "events": [event.as_dict() for event in prepared],
        }
        if dry_run:
            return report
        if not self.settings.tomoru_events_enabled:
            report["mode"] = "blocked"
            report["blocked_reason"] = "TOMORU_EVENTS_ENABLED=false"
            return report
        if not self.settings.tomoru_bot_id:
            report["mode"] = "blocked"
            report["blocked_reason"] = "TOMORU_BOT_ID is not configured"
            return report
        if not callback_url:
            report["mode"] = "blocked"
            report["blocked_reason"] = "Tomoru callback URL is not subscribed"
            return report

        target = validate_tomoru_callback_url(
            callback_url,
            self.settings.tomoru_callback_allowed_hosts,
        )
        report["mode"] = "live"
        report["external_calls"] = True
        entry_map = {entry.id: entry for entry in entries}
        for event in prepared:
            entry = entry_map[event.queue_entry_id]
            try:
                response = self._post(
                    target,
                    json=event.payload,
                    timeout=self.settings.tomoru_http_timeout_seconds,
                    headers={"Content-Type": "application/json"},
                )
                status_code = int(getattr(response, "status_code", 0) or 0)
                if not 200 <= status_code < 300:
                    body = str(getattr(response, "text", ""))[:500]
                    raise RuntimeError(f"Tomoru returned HTTP {status_code}: {body}")
                entry.status = "sent_to_tomoru"
                entry.dispatched_at = utcnow()
                entry.dispatched_campaign_id = str(event.payload["trackingId"])
                entry.last_error = None
                report["sent"] += 1
            except Exception as exc:
                entry.status = "failed"
                entry.last_error = str(exc)[:2000]
                report["failed"] += 1
        return report

    @staticmethod
    def _chat_uri(phone: str) -> str:
        digits = re.sub(r"\D+", "", str(phone or ""))
        if len(digits) == 11 and digits.startswith("8"):
            digits = "7" + digits[1:]
        if not digits:
            raise ValueError("Phone is required for Tomoru event")
        return f"tel://+{digits}"
