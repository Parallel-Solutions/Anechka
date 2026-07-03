"""Repository for normalized CRM contacts, phones and links."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CrmContact, CrmContactLink, CrmContactPhone, CrmEntity, utcnow
from app.services.phone_service import normalize_phone


class ContactRepository:
    def __init__(self, db: Session, portal_id: str):
        self.db = db
        self.portal_id = portal_id

    def upsert_contact(
        self,
        contact_id: int,
        fields: dict[str, Any],
        *,
        is_synthetic: bool = False,
        source_lead_id: int | None = None,
        raw_payload: dict | None = None,
    ) -> CrmContact:
        existing = self.db.scalar(
            select(CrmContact).where(
                CrmContact.portal_id == self.portal_id,
                CrmContact.contact_id == contact_id,
            )
        )
        now = utcnow()
        cols = (
            "last_name",
            "name",
            "second_name",
            "full_name",
            "post",
            "post_custom",
            "company_id",
            "company_title",
            "primary_phone",
            "primary_phone_type",
        )
        if existing is None:
            existing = CrmContact(
                portal_id=self.portal_id,
                contact_id=contact_id,
                is_synthetic=is_synthetic,
                source_lead_id=source_lead_id,
                raw_payload=raw_payload or {},
                first_imported_at=now,
            )
            for c in cols:
                setattr(existing, c, fields.get(c))
            self.db.add(existing)
        else:
            for c in cols:
                new_val = fields.get(c)
                if new_val not in (None, ""):
                    setattr(existing, c, new_val)
            if raw_payload:
                existing.raw_payload = raw_payload
            if source_lead_id is not None:
                existing.source_lead_id = source_lead_id
            existing.last_imported_at = now
        self.db.flush()
        return existing

    def sync_phones(
        self, contact_id: int, phones: list[dict[str, str]], primary_value: str | None
    ) -> None:
        """Upsert телефонов (дедуп по нормализованным цифрам), пометка основного."""
        now = utcnow()
        existing = list(
            self.db.scalars(
                select(CrmContactPhone).where(
                    CrmContactPhone.portal_id == self.portal_id,
                    CrmContactPhone.contact_id == contact_id,
                )
            )
        )
        by_norm: dict[str, CrmContactPhone] = {}
        for p in existing:
            norm = normalize_phone(p.value)
            if norm and norm not in by_norm:
                by_norm[norm] = p
        for p in existing:
            p.is_primary = False

        primary_norm = normalize_phone(primary_value) if primary_value else None

        for ph in phones:
            val, vt = ph["value"], ph["value_type"]
            norm = normalize_phone(val)
            if not norm:
                continue
            is_primary = primary_norm is not None and norm == primary_norm
            rec = by_norm.get(norm)
            if rec is None:
                rec = CrmContactPhone(
                    portal_id=self.portal_id,
                    contact_id=contact_id,
                    value=val,
                    value_type=vt,
                    is_primary=is_primary,
                    first_imported_at=now,
                )
                self.db.add(rec)
                by_norm[norm] = rec
            else:
                rec.value = val
                rec.value_type = vt
                rec.is_primary = is_primary
                rec.last_imported_at = now

        self.db.flush()

        all_rows = list(
            self.db.scalars(
                select(CrmContactPhone).where(
                    CrmContactPhone.portal_id == self.portal_id,
                    CrmContactPhone.contact_id == contact_id,
                )
            )
        )
        groups: dict[str, list[CrmContactPhone]] = {}
        for rec in all_rows:
            norm = normalize_phone(rec.value)
            if norm:
                groups.setdefault(norm, []).append(rec)
        for group in groups.values():
            if len(group) <= 1:
                continue
            keep = next((r for r in group if r.is_primary), group[-1])
            for rec in group:
                if rec is not keep:
                    self.db.delete(rec)

        self.db.flush()

    def upsert_link(
        self,
        contact_id: int,
        parent_entity_type_id: int,
        parent_entity_id: int,
        is_primary: bool,
    ) -> CrmContactLink:
        existing = self.db.scalar(
            select(CrmContactLink).where(
                CrmContactLink.portal_id == self.portal_id,
                CrmContactLink.contact_id == contact_id,
                CrmContactLink.parent_entity_type_id == parent_entity_type_id,
                CrmContactLink.parent_entity_id == parent_entity_id,
            )
        )
        now = utcnow()
        if existing is None:
            existing = CrmContactLink(
                portal_id=self.portal_id,
                contact_id=contact_id,
                parent_entity_type_id=parent_entity_type_id,
                parent_entity_id=parent_entity_id,
                is_primary=is_primary,
                first_imported_at=now,
            )
            self.db.add(existing)
        else:
            existing.is_primary = is_primary
            existing.last_imported_at = now
        self.db.flush()
        return existing

    def get_contacts_for_parent(
        self, parent_entity_type_id: int, parent_entity_id: int
    ) -> list[dict[str, Any]]:
        """Контакты, привязанные к сделке/лиду. Каждый элемент: {link, contact}."""
        rows = self.db.execute(
            select(CrmContactLink, CrmContact)
            .join(
                CrmContact,
                (CrmContact.portal_id == CrmContactLink.portal_id)
                & (CrmContact.contact_id == CrmContactLink.contact_id),
            )
            .where(
                CrmContactLink.portal_id == self.portal_id,
                CrmContactLink.parent_entity_type_id == parent_entity_type_id,
                CrmContactLink.parent_entity_id == parent_entity_id,
            )
            .order_by(CrmContactLink.is_primary.desc(), CrmContact.full_name)
        ).all()
        return [{"link": link, "contact": contact} for link, contact in rows]

    def get_contacts_for_parents(
        self, parent_entity_type_id: int, parent_entity_ids: list[int]
    ) -> dict[int, list[dict[str, Any]]]:
        if not parent_entity_ids:
            return {}
        rows = self.db.execute(
            select(CrmContactLink, CrmContact)
            .join(
                CrmContact,
                (CrmContact.portal_id == CrmContactLink.portal_id)
                & (CrmContact.contact_id == CrmContactLink.contact_id),
            )
            .where(
                CrmContactLink.portal_id == self.portal_id,
                CrmContactLink.parent_entity_type_id == parent_entity_type_id,
                CrmContactLink.parent_entity_id.in_(parent_entity_ids),
            )
            .order_by(CrmContactLink.is_primary.desc(), CrmContact.full_name)
        ).all()
        out: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for link, contact in rows:
            out[int(link.parent_entity_id)].append({"link": link, "contact": contact})
        return dict(out)

    def get_links_for_contact(self, contact_id: int) -> list[dict[str, Any]]:
        """Сделки и лиды, к которым привязан контакт. Каждый элемент: {link, parent}."""
        rows = self.db.execute(
            select(CrmContactLink, CrmEntity)
            .outerjoin(
                CrmEntity,
                (CrmEntity.portal_id == CrmContactLink.portal_id)
                & (CrmEntity.entity_type_id == CrmContactLink.parent_entity_type_id)
                & (CrmEntity.entity_id == CrmContactLink.parent_entity_id),
            )
            .where(
                CrmContactLink.portal_id == self.portal_id,
                CrmContactLink.contact_id == contact_id,
            )
            .order_by(
                CrmContactLink.parent_entity_type_id,
                CrmContactLink.is_primary.desc(),
            )
        ).all()
        return [{"link": link, "parent": parent} for link, parent in rows]

    def get_contacts_by_company_id(self, company_id: int) -> list[CrmContact]:
        if not company_id:
            return []
        return list(
            self.db.scalars(
                select(CrmContact).where(
                    CrmContact.portal_id == self.portal_id,
                    CrmContact.company_id == company_id,
                )
            )
        )

    def get_contacts_by_company_ids(
        self, company_ids: list[int]
    ) -> dict[int, list[CrmContact]]:
        if not company_ids:
            return {}
        rows = self.db.scalars(
            select(CrmContact).where(
                CrmContact.portal_id == self.portal_id,
                CrmContact.company_id.in_(company_ids),
            )
        ).all()
        out: dict[int, list[CrmContact]] = defaultdict(list)
        for contact in rows:
            if contact.company_id is not None:
                out[int(contact.company_id)].append(contact)
        return dict(out)

    def get_contact(self, contact_id: int) -> CrmContact | None:
        return self.db.scalar(
            select(CrmContact).where(
                CrmContact.portal_id == self.portal_id,
                CrmContact.contact_id == contact_id,
            )
        )

    def get_contacts(self, contact_ids: list[int]) -> dict[int, CrmContact]:
        if not contact_ids:
            return {}
        rows = self.db.scalars(
            select(CrmContact).where(
                CrmContact.portal_id == self.portal_id,
                CrmContact.contact_id.in_(contact_ids),
            )
        ).all()
        return {int(row.contact_id): row for row in rows}

    def get_phones_for_contact(self, contact_id: int) -> list[dict[str, str]]:
        """Phones from crm_contact_phones, fallback to raw_payload PHONE multifield."""
        rows = list(
            self.db.scalars(
                select(CrmContactPhone).where(
                    CrmContactPhone.portal_id == self.portal_id,
                    CrmContactPhone.contact_id == contact_id,
                )
            )
        )
        if rows:
            return [{"value": p.value, "value_type": p.value_type} for p in rows]
        contact = self.get_contact(contact_id)
        if contact is None:
            return []
        from app.services.bitrix_import.contact_parser import parse_phones

        raw = (contact.raw_payload or {}).get("PHONE") or (contact.raw_payload or {}).get("phone")
        return parse_phones(raw)

    def get_phones_for_contacts(
        self, contact_ids: list[int]
    ) -> dict[int, list[dict[str, str]]]:
        if not contact_ids:
            return {}
        rows = list(
            self.db.scalars(
                select(CrmContactPhone).where(
                    CrmContactPhone.portal_id == self.portal_id,
                    CrmContactPhone.contact_id.in_(contact_ids),
                )
            )
        )
        out: dict[int, list[dict[str, str]]] = defaultdict(list)
        for phone in rows:
            out[int(phone.contact_id)].append(
                {"value": phone.value, "value_type": phone.value_type}
            )
        missing = [cid for cid in contact_ids if cid not in out]
        if not missing:
            return dict(out)
        contacts = self.get_contacts(missing)
        from app.services.bitrix_import.contact_parser import parse_phones

        for contact_id, contact in contacts.items():
            raw = (contact.raw_payload or {}).get("PHONE") or (
                contact.raw_payload or {}
            ).get("phone")
            parsed = parse_phones(raw)
            if parsed:
                out[contact_id] = parsed
        return dict(out)
