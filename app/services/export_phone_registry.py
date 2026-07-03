"""Persist and lookup phone hashes from Tomoru/LPR exports."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ExportJob, ExportPhoneEntry
from app.services.phone_service import normalize_phone, phone_hash


@dataclass
class ExportPhoneRow:
    phone: str
    deal_id: int
    contact_id: int | None = None


@dataclass
class ExportPhoneHistoryItem:
    export_job_id: int
    export_mode: str
    job_mode: str
    status: str
    created_at: datetime
    finished_at: datetime | None
    deal_id: int
    contact_id: int | None
    parameters: dict[str, Any]


class _LprReportRowLike(Protocol):
    phone: str
    deal_id: int
    contact_id: int | None


class ExportPhoneRegistry:
    def __init__(self, db: Session):
        self.db = db

    def save_entries(
        self,
        portal_id: str,
        export_job_id: int,
        mode: str,
        rows: list[ExportPhoneRow | dict[str, Any] | _LprReportRowLike],
    ) -> int:
        seen_hashes: set[str] = set()
        to_add: list[ExportPhoneEntry] = []
        for row in rows:
            if isinstance(row, ExportPhoneRow):
                phone = row.phone
                deal_id = row.deal_id
                contact_id = row.contact_id
            elif isinstance(row, dict):
                phone = str(row.get("phone") or "")
                deal_id = int(row["deal_id"])
                contact_id = row.get("contact_id")
                if contact_id is not None:
                    contact_id = int(contact_id)
            else:
                phone = row.phone
                deal_id = int(row.deal_id)
                contact_id = row.contact_id

            normalized = normalize_phone(phone)
            if not normalized:
                continue
            digest = phone_hash(normalized)
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            to_add.append(
                ExportPhoneEntry(
                    portal_id=portal_id,
                    export_job_id=export_job_id,
                    phone_hash=digest,
                    normalized_phone=normalized,
                    deal_id=deal_id,
                    contact_id=contact_id,
                    export_mode=mode,
                )
            )

        if not to_add:
            return 0

        self.db.add_all(to_add)
        self.db.flush()
        return len(to_add)

    def save_from_lpr_report(
        self,
        portal_id: str,
        export_job_id: int,
        rows: list[_LprReportRowLike],
    ) -> int:
        return self.save_entries(portal_id, export_job_id, "region_lpr", rows)

    def lookup(self, portal_id: str, normalized_phone: str) -> list[ExportPhoneEntry]:
        canonical = normalize_phone(normalized_phone)
        if not canonical:
            return []

        digest = phone_hash(canonical)
        rows = list(
            self.db.scalars(
                select(ExportPhoneEntry)
                .where(
                    ExportPhoneEntry.portal_id == portal_id,
                    ExportPhoneEntry.phone_hash == digest,
                )
                .order_by(ExportPhoneEntry.created_at.desc(), ExportPhoneEntry.id.desc())
            )
        )
        if rows:
            return rows

        rows = list(
            self.db.scalars(
                select(ExportPhoneEntry)
                .where(
                    ExportPhoneEntry.portal_id == portal_id,
                    ExportPhoneEntry.normalized_phone == canonical,
                )
                .order_by(ExportPhoneEntry.created_at.desc(), ExportPhoneEntry.id.desc())
            )
        )
        if rows:
            return rows

        if len(canonical) == 11 and canonical.startswith("7"):
            return list(
                self.db.scalars(
                    select(ExportPhoneEntry)
                    .where(
                        ExportPhoneEntry.portal_id == portal_id,
                        ExportPhoneEntry.normalized_phone == canonical[1:],
                    )
                    .order_by(ExportPhoneEntry.created_at.desc(), ExportPhoneEntry.id.desc())
                )
            )
        return []

    def lookup_export_history(
        self,
        portal_id: str,
        phone: str,
    ) -> tuple[str | None, list[ExportPhoneHistoryItem]]:
        canonical = normalize_phone(phone)
        if not canonical:
            return None, []

        entries = self.lookup(portal_id, canonical)
        if not entries:
            return canonical, []

        job_ids = {entry.export_job_id for entry in entries}
        jobs = {
            job.id: job
            for job in self.db.scalars(select(ExportJob).where(ExportJob.id.in_(job_ids)))
        }

        items: list[ExportPhoneHistoryItem] = []
        for entry in entries:
            job = jobs.get(entry.export_job_id)
            if not job:
                continue
            try:
                parameters = json.loads(job.parameters_json or "{}")
            except json.JSONDecodeError:
                parameters = {}
            items.append(
                ExportPhoneHistoryItem(
                    export_job_id=entry.export_job_id,
                    export_mode=entry.export_mode,
                    job_mode=job.mode,
                    status=job.status,
                    created_at=job.created_at,
                    finished_at=job.finished_at,
                    deal_id=entry.deal_id,
                    contact_id=entry.contact_id,
                    parameters=parameters,
                )
            )
        return canonical, items
