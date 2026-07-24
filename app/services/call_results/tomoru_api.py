"""Client and guarded batch dispatcher for the current Tomoru REST API."""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from app.config import Settings
from app.models import CallRetryQueueEntry, utcnow
from app.services.call_results.tomoru_retry_campaign import (
    TomoruCampaignDraft,
    TomoruRetryCampaignPlanner,
)
from app.services.call_results.tomoru_initial_campaign import (
    TomoruInitialBatchDraft,
    TomoruInitialCampaignPlanner,
)


class TomoruApiError(RuntimeError):
    """Raised when Tomoru rejects a request or returns an invalid response."""


class TomoruApiClient:
    """Small explicit client for the documented Tomoru batch endpoints."""

    def __init__(
        self,
        settings: Settings,
        *,
        request: Callable[..., Any] | None = None,
    ):
        self.settings = settings
        self.base_url = settings.tomoru_api_base_url.rstrip("/")
        self._request_transport = request or requests.request

    def list_agents(self) -> dict[str, Any]:
        return self._request("GET", "/api/agents")

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/call_batches/{batch_id}")

    def create_batch(self, draft: TomoruCampaignDraft) -> dict[str, Any]:
        return self._create_uploaded_batch(
            name=draft.campaign_name,
            csv_bytes=self._build_csv(draft),
            scheduled_at=draft.scheduled_at,
        )

    def create_initial_batch(self, draft: TomoruInitialBatchDraft) -> dict[str, Any]:
        return self._create_uploaded_batch(
            name=draft.campaign_name,
            csv_bytes=self._build_initial_csv(draft),
            scheduled_at=draft.scheduled_at,
        )

    def _create_uploaded_batch(
        self,
        *,
        name: str,
        csv_bytes: bytes,
        scheduled_at: datetime,
    ) -> dict[str, Any]:
        start_at = scheduled_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        data = {
            "name": name,
            "agent_id": self.settings.tomoru_agent_id,
            "max_retries": str(max(0, self.settings.tomoru_batch_max_retries)),
            "retry_delay_seconds": str(
                max(0, self.settings.tomoru_batch_retry_delay_seconds)
            ),
            "start_at": start_at,
        }
        if self.settings.tomoru_result_callback_url:
            data["callback_url"] = self.settings.tomoru_result_callback_url
        return self._request(
            "POST",
            "/api/call_batches",
            data=data,
            files={"csv_file": (f"{name}.csv", csv_bytes, "text/csv")},
        )

    def launch_batch(self, batch_id: str) -> dict[str, Any]:
        return self._request("PATCH", f"/api/call_batches/{batch_id}/launch")

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if not self.settings.tomoru_api_key:
            raise TomoruApiError("TOMORU_API_KEY is not configured")
        response = self._request_transport(
            method,
            f"{self.base_url}{path}",
            headers={
                "Authorization": f"Bearer {self.settings.tomoru_api_key}",
                "Accept": "application/json",
            },
            timeout=self.settings.tomoru_http_timeout_seconds,
            **kwargs,
        )
        status_code = int(getattr(response, "status_code", 0) or 0)
        if not 200 <= status_code < 300:
            body = str(getattr(response, "text", ""))[:1000]
            raise TomoruApiError(f"Tomoru returned HTTP {status_code}: {body}")
        try:
            payload = response.json()
        except Exception as exc:
            raise TomoruApiError("Tomoru returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise TomoruApiError("Tomoru returned an unexpected response")
        return payload

    @classmethod
    def _build_csv(cls, draft: TomoruCampaignDraft) -> bytes:
        output = io.StringIO(newline="")
        fieldnames = [
            "phone_number",
            "queue_entry_id",
            "reason",
            "timezone",
            "deal_id",
            "contact_id",
            "callback_text",
            "phone_extension",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for contact in draft.contacts:
            writer.writerow(
                {
                    "phone_number": cls._e164(contact.phone_number),
                    "queue_entry_id": contact.queue_entry_id,
                    "reason": draft.reason,
                    "timezone": draft.timezone,
                    "deal_id": contact.deal_id or "",
                    "contact_id": contact.contact_id or "",
                    "callback_text": contact.callback_text or "",
                    "phone_extension": contact.phone_extension or "",
                }
            )
        return output.getvalue().encode("utf-8-sig")

    @classmethod
    def _build_initial_csv(cls, draft: TomoruInitialBatchDraft) -> bytes:
        output = io.StringIO(newline="")
        fieldnames = [
            "phone_number",
            "timezone",
            "deal_id",
            "contact_id",
            "fio",
            "company",
            "deal_title",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for row in draft.rows:
            writer.writerow(
                {
                    "phone_number": cls._e164(getattr(row, "phone", "")),
                    "timezone": draft.timezone,
                    "deal_id": getattr(row, "deal_id", "") or "",
                    "contact_id": getattr(row, "contact_id", "") or "",
                    "fio": getattr(row, "fio", "") or "",
                    "company": getattr(row, "company", "") or "",
                    "deal_title": getattr(row, "deal_title", "") or "",
                }
            )
        return output.getvalue().encode("utf-8-sig")

    @staticmethod
    def _e164(phone: str) -> str:
        digits = re.sub(r"\D+", "", str(phone or ""))
        if len(digits) == 11 and digits.startswith("8"):
            digits = "7" + digits[1:]
        if not digits:
            raise TomoruApiError("Phone is required for Tomoru batch")
        return f"+{digits}"


class TomoruBatchDispatcher:
    """Create Tomoru batches only behind explicit creation/launch flags."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: TomoruApiClient | None = None,
    ):
        self.settings = settings
        self.client = client or TomoruApiClient(settings)
        self.planner = TomoruRetryCampaignPlanner(settings.tomoru_default_local_call_time)

    def dispatch(
        self,
        entries: list[CallRetryQueueEntry],
        *,
        dry_run: bool = True,
        launch: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        candidates = [
            entry
            for entry in entries
            if not entry.dispatched_campaign_id
        ]
        drafts = self.planner.plan(candidates, now=current)
        report: dict[str, Any] = {
            "mode": "dry_run",
            "external_calls": False,
            "launch_requested": launch,
            "prepared_batches": len(drafts),
            "prepared_contacts": sum(len(draft.contacts) for draft in drafts),
            "created_batches": 0,
            "launched_batches": 0,
            "failed_batches": 0,
            "batches": [draft.as_dict() for draft in drafts],
        }
        if dry_run:
            return report
        if not self.settings.tomoru_batch_creation_enabled:
            report["mode"] = "blocked"
            report["blocked_reason"] = "TOMORU_BATCH_CREATION_ENABLED=false"
            return report
        if not self.settings.tomoru_api_key:
            report["mode"] = "blocked"
            report["blocked_reason"] = "TOMORU_API_KEY is not configured"
            return report
        if not self.settings.tomoru_agent_id:
            report["mode"] = "blocked"
            report["blocked_reason"] = "TOMORU_AGENT_ID is not configured"
            return report
        if launch and not self.settings.tomoru_batch_auto_launch_enabled:
            report["mode"] = "blocked"
            report["blocked_reason"] = "TOMORU_BATCH_AUTO_LAUNCH_ENABLED=false"
            return report

        report["mode"] = "live"
        report["external_calls"] = True
        entry_by_id = {entry.id: entry for entry in candidates}
        report["batches"] = []
        for draft in drafts:
            draft_report = {
                "idempotency_key": draft.idempotency_key,
                "campaign_name": draft.campaign_name,
                "scheduled_at": draft.scheduled_at.isoformat(),
                "contact_count": len(draft.contacts),
                "batch_id": None,
                "launched": False,
                "error": None,
            }
            try:
                created = self.client.create_batch(draft)
                batch_id = str(created.get("id") or "")
                if not batch_id:
                    raise TomoruApiError("Tomoru response does not contain batch id")
                draft_report["batch_id"] = batch_id
                report["created_batches"] += 1
                for contact in draft.contacts:
                    entry = entry_by_id[contact.queue_entry_id]
                    entry.status = "scheduled"
                    entry.dispatched_campaign_id = batch_id
                    entry.dispatched_at = utcnow()
                    entry.last_error = None
                if launch:
                    self.client.launch_batch(batch_id)
                    draft_report["launched"] = True
                    report["launched_batches"] += 1
                    for contact in draft.contacts:
                        entry_by_id[contact.queue_entry_id].status = "sent_to_tomoru"
            except Exception as exc:
                error = str(exc)[:2000]
                draft_report["error"] = error
                report["failed_batches"] += 1
                for contact in draft.contacts:
                    entry = entry_by_id[contact.queue_entry_id]
                    if not entry.dispatched_campaign_id:
                        entry.status = "failed"
                    entry.last_error = error
            report["batches"].append(draft_report)
        return report

class TomoruInitialBatchDispatcher:
    """Create initial export batches and only launch behind the global safety flag."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: TomoruApiClient | None = None,
    ):
        self.settings = settings
        self.client = client or TomoruApiClient(settings)
        self.planner = TomoruInitialCampaignPlanner()

    def dispatch(
        self,
        rows: list[Any],
        *,
        local_call_time: str = "10:00",
        launch: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        drafts = self.planner.plan(rows, local_call_time=local_call_time, now=now)
        report: dict[str, Any] = {
            "mode": "live",
            "external_calls": False,
            "launch_requested": launch,
            "prepared_batches": len(drafts),
            "prepared_contacts": sum(len(draft.rows) for draft in drafts),
            "created_batches": 0,
            "launched_batches": 0,
            "failed_batches": 0,
            "batches": [],
        }
        if not self.settings.tomoru_batch_creation_enabled:
            report["mode"] = "blocked"
            report["blocked_reason"] = "TOMORU_BATCH_CREATION_ENABLED=false"
            return report
        if not self.settings.tomoru_api_key:
            report["mode"] = "blocked"
            report["blocked_reason"] = "TOMORU_API_KEY is not configured"
            return report
        if not self.settings.tomoru_agent_id:
            report["mode"] = "blocked"
            report["blocked_reason"] = "TOMORU_AGENT_ID is not configured"
            return report
        if launch and not self.settings.tomoru_batch_auto_launch_enabled:
            report["mode"] = "blocked"
            report["blocked_reason"] = "TOMORU_BATCH_AUTO_LAUNCH_ENABLED=false"
            return report

        report["external_calls"] = True
        for draft in drafts:
            item = draft.as_dict() | {
                "batch_id": None,
                "launched": False,
                "error": None,
            }
            try:
                created = self.client.create_initial_batch(draft)
                batch_id = str(created.get("id") or "")
                if not batch_id:
                    raise TomoruApiError("Tomoru response does not contain batch id")
                item["batch_id"] = batch_id
                report["created_batches"] += 1
                if launch:
                    self.client.launch_batch(batch_id)
                    item["launched"] = True
                    report["launched_batches"] += 1
            except Exception as exc:
                item["error"] = str(exc)[:2000]
                report["failed_batches"] += 1
            report["batches"].append(item)
        return report
