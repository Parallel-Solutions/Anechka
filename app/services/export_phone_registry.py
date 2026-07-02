"""Persist and lookup phone hashes from Tomoru/LPR exports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ExportPhoneEntry
from app.services.phone_service import normalize_phone, phone_hash


@dataclass
class ExportPhoneRow:
    phone: str
    deal_id: int
    contact_id: int | None = None


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
