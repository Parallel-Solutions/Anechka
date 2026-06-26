"""Repository for normalized CRM contacts, phones and links."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CrmContact, CrmContactLink, CrmContactPhone, CrmEntity, utcnow


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
        """Upsert телефонов (дедуп по value), пометка основного. Существующие не удаляем."""
        now = utcnow()
        existing = list(
            self.db.scalars(
                select(CrmContactPhone).where(
                    CrmContactPhone.portal_id == self.portal_id,
                    CrmContactPhone.contact_id == contact_id,
                )
            )
        )
        by_value = {p.value: p for p in existing}
        for p in existing:
            p.is_primary = False
        for ph in phones:
            val, vt = ph["value"], ph["value_type"]
            rec = by_value.get(val)
            is_primary = val == primary_value
            if rec is None:
                self.db.add(
                    CrmContactPhone(
                        portal_id=self.portal_id,
                        contact_id=contact_id,
                        value=val,
                        value_type=vt,
                        is_primary=is_primary,
                        first_imported_at=now,
                    )
                )
            else:
                rec.value_type = vt
                rec.is_primary = is_primary
                rec.last_imported_at = now
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

    def get_contact(self, contact_id: int) -> CrmContact | None:
        return self.db.scalar(
            select(CrmContact).where(
                CrmContact.portal_id == self.portal_id,
                CrmContact.contact_id == contact_id,
            )
        )

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
