"""Live Bitrix enrichment: company contacts for Tomoru export."""

from __future__ import annotations

import logging
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import CrmEntity
from app.repositories.contact_repository import ContactRepository
from app.services.bitrix_import.contact_parser import extract_contact_fields
from app.services.intelligent_export.contact_phone_heuristic import _deal_company_id

logger = logging.getLogger(__name__)


def _default_bitrix_client(settings: Settings) -> Any | None:
    if not settings.bitrix_webhook_url:
        return None
    from app.services.bitrix_client import BitrixClient

    return BitrixClient(settings)


def save_contact_from_bitrix(
    db: Session,
    portal_id: str,
    contact: dict[str, Any],
    *,
    bitrix_client: Any | None = None,
) -> None:
    """Persist a Bitrix contact card into crm_contacts + crm_contact_phones."""
    contact_id = int(contact.get("ID") or contact.get("id") or 0)
    if not contact_id:
        return
    fields = extract_contact_fields(contact)
    if fields["company_id"] and bitrix_client is not None:
        company = bitrix_client.get_company(fields["company_id"])
        if company:
            fields["company_title"] = (
                company.get("TITLE") or company.get("title") or fields["company_title"]
            )
    repo = ContactRepository(db, portal_id)
    repo.upsert_contact(contact_id, fields, raw_payload=contact)
    repo.sync_phones(contact_id, fields["phones"], fields["primary_phone"])


def enrich_company_contacts_for_deals(
    db: Session,
    portal_id: str,
    deal_rows: list[dict[str, Any]],
    *,
    deal_alias: str = "deal",
    settings: Settings | None = None,
    bitrix_client: Any | None = None,
    log: Callable[[str], None] | None = None,
) -> int:
    """Fetch company contacts from Bitrix and save to DB for Tomoru phone resolution."""
    _log = log or (lambda _m: None)
    cfg = settings or get_settings()
    client = bitrix_client or _default_bitrix_client(cfg)
    if client is None:
        _log("Bitrix webhook не настроен — пропуск live-обогащения контактами компании")
        return 0

    company_ids: set[int] = set()
    for row in deal_rows:
        deal = row.get(deal_alias)
        if isinstance(deal, CrmEntity):
            cid = _deal_company_id(deal)
            if cid:
                company_ids.add(cid)

    if not company_ids:
        return 0

    saved = 0
    for company_id in sorted(company_ids):
        try:
            contact_ids = client.get_company_contacts(company_id)
        except Exception:
            logger.exception("Failed to fetch company contacts for company %s", company_id)
            _log(f"Ошибка загрузки контактов компании {company_id}")
            continue
        for contact_id in contact_ids:
            try:
                contact = client.get_contact(contact_id)
                if not contact:
                    continue
                save_contact_from_bitrix(db, portal_id, contact, bitrix_client=client)
                saved += 1
            except Exception:
                logger.exception("Failed to save company contact %s", contact_id)
        if contact_ids:
            _log(f"Компания {company_id}: сохранено контактов {len(contact_ids)}")

    if saved:
        db.commit()
    return saved
