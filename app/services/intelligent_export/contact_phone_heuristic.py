"""Tomoru phone export heuristic: one contact + one phone per deal."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import ENTITY_COMPANY, ENTITY_DEAL, CrmContact, CrmContactLink, CrmEntity
from app.repositories.contact_repository import ContactRepository
from app.services.bitrix_import.contact_parser import choose_primary_phone, parse_phones
from app.services.export_plan.models_v2 import SheetPostProcess
from app.services.export_plan.payload_keys import payload_lookup
from app.services.intelligent_export.contact_lpr_classifier import (
    ContactLprClassifier,
    build_lpr_classifier,
)
from app.services.lpr_service import DEFAULT_LPR_STOPWORDS, LprConfig, contact_to_lpr_dict
from app.services.phone_service import normalize_phone

logger = logging.getLogger(__name__)

TOMORU_DEFAULT_CATEGORY_ID = 15
TOMORU_REGION_FIELD = "UF_CRM_5ECE25C5D78E0"

# Title fallbacks when UF region is empty on legacy cards (region list element id -> substrings).
REGION_TITLE_SYNONYMS: dict[int, tuple[str, ...]] = {
    1107: ("СПб", "Санкт-Петербург", "Петербург"),
    1105: ("Москва",),
    1091: ("Томск", "Томская"),
    1089: ("Тверск",),
    1007: ("Амурск",),
}

from app.services.intelligent_export.kp_legacy_stages import LEGACY_KP_STAGE_IDS

_ARCHITECT_KEYWORDS = (
    "архитектор",
    "architect",
    "арх.",
    "арх ",
    "отдел арх",
    "главный арх",
    "глав. арх",
    "архитектур",
)

_ARCHITECT_RE = re.compile(
    r"|".join(re.escape(kw) for kw in _ARCHITECT_KEYWORDS),
    re.I,
)


@dataclass
class ContactCandidate:
    contact: CrmContact
    link: CrmContactLink | None = None
    source: str = "deal"

    @property
    def contact_id(self) -> int:
        return int(self.contact.contact_id)

    def searchable_text(self) -> str:
        parts = [
            self.contact.full_name or "",
            self.contact.name or "",
            self.contact.last_name or "",
            self.contact.post or "",
            self.contact.post_custom or "",
            self.contact.company_title or "",
        ]
        raw = self.contact.raw_payload or {}
        parts.append(str(payload_lookup(raw, "COMMENTS") or ""))
        parts.append(str(payload_lookup(raw, "TITLE") or ""))
        return " ".join(p for p in parts if p).lower()

    def to_classifier_dict(self) -> dict[str, Any]:
        raw = self.contact.raw_payload or {}
        emails = payload_lookup(raw, "EMAIL") or payload_lookup(raw, "email")
        email = ""
        if isinstance(emails, list) and emails:
            first = emails[0]
            email = first.get("VALUE") or first.get("value") or str(first) if isinstance(first, dict) else str(first)
        elif emails:
            email = str(emails)
        payload = contact_to_lpr_dict(self.contact)
        payload.update(
            {
                "contact_id": self.contact_id,
                "full_name": self.contact.full_name or "",
                "company": self.contact.company_title or "",
                "email": email,
                "source": self.source,
            }
        )
        return payload

    def sort_key(self) -> tuple[float, float]:
        raw = self.contact.raw_payload or {}
        created = _parse_dt(payload_lookup(raw, "DATE_CREATE"))
        link_ts = self.link.first_imported_at if self.link else None
        c_ts = created.timestamp() if created else 0.0
        l_ts = link_ts.timestamp() if link_ts else 0.0
        return (c_ts, l_ts)


@dataclass
class PickResult:
    contact_id: int | None
    phone: str | None
    reason: str = ""


@dataclass
class TomoruBuildStats:
    deals_total: int = 0
    deals_skipped_archived: int = 0
    deals_skipped_wrong_category: int = 0
    deals_skipped_no_contact: int = 0
    deals_skipped_no_phone: int = 0
    deals_used_company_phone: int = 0
    phones_deduped: int = 0


def deal_category_id(deal: CrmEntity) -> int | None:
    if deal.category_id is not None and deal.category_id > 0:
        return int(deal.category_id)
    raw = deal.raw_payload or {}
    val = payload_lookup(raw, "CATEGORY_ID")
    if val in (None, "", "0", 0):
        return None
    try:
        parsed = int(val)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def is_deal_in_category(
    deal: CrmEntity,
    category_id: int,
    *,
    legacy_stage_ids: frozenset[str] | None = None,
) -> bool:
    actual = deal_category_id(deal)
    if actual is not None:
        return actual == category_id
    if category_id != TOMORU_DEFAULT_CATEGORY_ID:
        return False
    stage = (deal.stage_id or "").strip()
    if not stage or ":" in stage:
        return False
    legacy = legacy_stage_ids if legacy_stage_ids is not None else LEGACY_KP_STAGE_IDS
    return stage in legacy


def is_deal_archived(
    deal: CrmEntity,
    *,
    archive_stage_ids: frozenset[str] | None = None,
) -> bool:
    raw = deal.raw_payload or {}
    for key in ("closed", "CLOSED"):
        val = raw.get(key)
        if val is not None and str(val).upper() == "Y":
            return True
    stage = (deal.stage_id or "").strip()
    if not stage:
        stage = str(raw.get("stageId") or raw.get("STAGE_ID") or "").strip()
    if archive_stage_ids and stage in archive_stage_ids:
        return True
    return False


def filter_non_archived_deals(
    deals: list[CrmEntity],
    *,
    archive_stage_ids: frozenset[str] | None = None,
) -> list[CrmEntity]:
    if not archive_stage_ids:
        return deals
    return [d for d in deals if not is_deal_archived(d, archive_stage_ids=archive_stage_ids)]


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text.replace("+03:00", "+0300")[:25], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _deal_company_id(deal: CrmEntity) -> int | None:
    raw = deal.raw_payload or {}
    val = payload_lookup(raw, "COMPANY_ID")
    if val in (None, "", "0", 0):
        return None
    try:
        cid = int(val)
        return cid if cid > 0 else None
    except (TypeError, ValueError):
        return None


def _deal_contact_ids(deal: CrmEntity) -> list[int]:
    """Contact IDs from deal payload when crm_contact_links are missing."""
    raw = deal.raw_payload or {}
    out: list[int] = []
    seen: set[int] = set()

    def add(val: Any) -> None:
        if val in (None, "", "0", 0):
            return
        try:
            cid = int(val)
        except (TypeError, ValueError):
            return
        if cid > 0 and cid not in seen:
            seen.add(cid)
            out.append(cid)

    add(payload_lookup(raw, "CONTACT_ID"))
    contact_ids = payload_lookup(raw, "CONTACT_IDS") or payload_lookup(raw, "contactIds")
    if isinstance(contact_ids, list):
        for item in contact_ids:
            add(item)
    return out


def _has_stopword(text: str, stopwords: list[str]) -> bool:
    hay = text.lower()
    return any(sw.lower() in hay for sw in stopwords if sw)


def detect_architect(candidate: ContactCandidate, *, stopwords: list[str] | None = None) -> bool:
    text = candidate.searchable_text()
    if _has_stopword(text, stopwords or DEFAULT_LPR_STOPWORDS):
        return False
    return bool(_ARCHITECT_RE.search(text))


def collect_deal_contacts(
    db: Session,
    portal_id: str,
    deal: CrmEntity,
    *,
    include_company_contacts: bool,
) -> list[ContactCandidate]:
    repo = ContactRepository(db, portal_id)
    seen: set[int] = set()
    out: list[ContactCandidate] = []

    def add(contact: CrmContact, link: CrmContactLink | None, source: str) -> None:
        cid = int(contact.contact_id)
        if cid in seen:
            return
        seen.add(cid)
        out.append(ContactCandidate(contact=contact, link=link, source=source))

    for row in repo.get_contacts_for_parent(ENTITY_DEAL, int(deal.entity_id)):
        contact = row.get("contact")
        if contact is not None:
            add(contact, row.get("link"), "deal")

    for contact_id in _deal_contact_ids(deal):
        if contact_id in seen:
            continue
        contact = repo.get_contact(contact_id)
        if contact is not None:
            add(contact, None, "deal")

    if include_company_contacts:
        company_id = _deal_company_id(deal)
        if company_id:
            for contact in repo.get_contacts_by_company_id(company_id):
                add(contact, None, "company")

    return out


def pick_phone_for_contact(db: Session, portal_id: str, contact_id: int) -> str | None:
    repo = ContactRepository(db, portal_id)
    phones = repo.get_phones_for_contact(contact_id)
    primary = choose_primary_phone(phones)
    if not primary:
        return None
    return normalize_phone(primary["value"])


def _phones_from_entity_payload(raw: dict[str, Any]) -> list[dict[str, str]]:
    phones = parse_phones(payload_lookup(raw, "PHONE"))
    if phones:
        return phones
    fm = payload_lookup(raw, "FM")
    if isinstance(fm, list):
        phone_items = [
            item
            for item in fm
            if isinstance(item, dict)
            and str(item.get("typeId") or item.get("TYPE_ID") or "").upper() == "PHONE"
        ]
        phones = parse_phones(phone_items)
        if phones:
            return phones
    return []


def pick_company_phone(
    db: Session,
    portal_id: str,
    company_id: int,
    *,
    bitrix_client: Any | None = None,
) -> str | None:
    """Primary phone from imported company entity, with optional live Bitrix fallback."""
    from app.repositories.crm_repository import CrmRepository

    crm_repo = CrmRepository(db, portal_id)
    entity = crm_repo.get_entity(ENTITY_COMPANY, company_id)
    if entity is not None:
        raw = entity.raw_payload or {}
        primary = choose_primary_phone(_phones_from_entity_payload(raw))
        if primary:
            normalized = normalize_phone(primary["value"])
            if normalized:
                return normalized

    if bitrix_client is not None:
        try:
            company = bitrix_client.get_company(company_id)
            if company:
                primary = choose_primary_phone(_phones_from_entity_payload(company))
                if primary:
                    return normalize_phone(primary["value"])
        except Exception:
            logger.exception("Failed to fetch company phone for company %s", company_id)
    return None


def pick_contact_for_deal(
    candidates: list[ContactCandidate],
    *,
    lpr_config: LprConfig,
    classifier: ContactLprClassifier,
    deal_title: str = "",
) -> tuple[ContactCandidate | None, str]:
    if not candidates:
        return None, ""

    active = [c for c in candidates if not _has_stopword(c.searchable_text(), lpr_config.stopwords)]
    pool = active or candidates

    architects = [c for c in pool if detect_architect(c, stopwords=lpr_config.stopwords)]
    if architects:
        chosen = architects[0]
        return chosen, f"архитектор ({chosen.source})"

    lpr_result = classifier.pick_lpr(pool, deal_title=deal_title)
    if lpr_result.contact_id is not None:
        match = next((c for c in pool if c.contact_id == lpr_result.contact_id), None)
        if match:
            return match, lpr_result.reason or "ЛПР"

    last = max(pool, key=lambda c: c.sort_key())
    return last, "последний добавленный контакт"


def pick_phone_for_deal(
    db: Session,
    portal_id: str,
    deal: CrmEntity,
    *,
    post_process: SheetPostProcess,
    lpr_config: LprConfig,
    classifier: ContactLprClassifier,
    bitrix_client: Any | None = None,
) -> PickResult:
    candidates = collect_deal_contacts(
        db,
        portal_id,
        deal,
        include_company_contacts=post_process.include_company_contacts,
    )
    contact, reason = pick_contact_for_deal(
        candidates,
        lpr_config=lpr_config,
        classifier=classifier,
        deal_title=deal.title or "",
    )
    phone: str | None = None
    contact_id: int | None = None
    if contact is not None:
        contact_id = contact.contact_id
        phone = pick_phone_for_contact(db, portal_id, contact.contact_id)

    if not phone and post_process.include_company_phones:
        company_id = _deal_company_id(deal)
        if company_id:
            phone = pick_company_phone(db, portal_id, company_id, bitrix_client=bitrix_client)
            if phone:
                reason = "телефон компании"
                contact_id = None

    return PickResult(contact_id, phone, reason)


def build_tomoru_phone_rows(
    db: Session,
    portal_id: str,
    deal_rows: list[dict[str, Any]],
    *,
    post_process: SheetPostProcess,
    lpr_config: LprConfig,
    settings: Settings,
    classifier: ContactLprClassifier | None = None,
    log: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, Any]], TomoruBuildStats]:
    """Build output rows ``[{phone: '7...'}]`` from fetched deal entity rows."""
    from app.services.intelligent_export.kp_legacy_stages import legacy_kp_stage_ids
    from app.services.intelligent_export.tomoru_stages import resolve_archive_stage_ids

    _log = log or (lambda _m: None)
    stats = TomoruBuildStats(deals_total=len(deal_rows))
    legacy_stages = legacy_kp_stage_ids(db, portal_id)
    lpr = classifier or build_lpr_classifier(settings, lpr_config, use_llm=post_process.use_llm_for_lpr)
    bitrix_client = None
    if post_process.include_company_phones or post_process.fetch_company_contacts_live:
        if settings.bitrix_webhook_url:
            from app.services.bitrix_client import BitrixClient

            bitrix_client = BitrixClient(settings)
    archive_stages = (
        resolve_archive_stage_ids(db, portal_id, post_process.category_id, client=bitrix_client)
        if post_process.exclude_archived
        else frozenset()
    )
    seen_phones: set[str] = set()
    out: list[dict[str, Any]] = []
    alias = post_process.deal_alias

    for row in deal_rows:
        deal = row.get(alias)
        if not isinstance(deal, CrmEntity):
            continue
        if post_process.exclude_archived and is_deal_archived(deal, archive_stage_ids=archive_stages):
            stats.deals_skipped_archived += 1
            continue
        if not is_deal_in_category(deal, post_process.category_id, legacy_stage_ids=legacy_stages):
            stats.deals_skipped_wrong_category += 1
            continue

        pick = pick_phone_for_deal(
            db,
            portal_id,
            deal,
            post_process=post_process,
            lpr_config=lpr_config,
            classifier=lpr,
            bitrix_client=bitrix_client,
        )
        if not pick.phone:
            if pick.contact_id is None and pick.reason != "телефон компании":
                stats.deals_skipped_no_contact += 1
                _log(f"Сделка {deal.entity_id}: контакт не выбран, телефон компании отсутствует")
            else:
                stats.deals_skipped_no_phone += 1
                _log(f"Сделка {deal.entity_id}: телефон не найден")
            continue
        if pick.reason == "телефон компании":
            stats.deals_used_company_phone += 1
        if post_process.deduplicate_phones and pick.phone in seen_phones:
            stats.phones_deduped += 1
            continue
        seen_phones.add(pick.phone)
        out.append({"phone": pick.phone})
        _log(f"Сделка {deal.entity_id}: {pick.phone} ({pick.reason})")

    return out, stats
