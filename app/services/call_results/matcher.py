"""Match call result rows to CRM contacts and deals."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CrmContact,
    CrmContactLink,
    CrmContactPhone,
    CrmEntity,
    CrmUser,
    ENTITY_COMPANY,
    ENTITY_DEAL,
)
from app.services.export_phone_registry import ExportPhoneRegistry
from app.services.intelligent_export.contact_phone_heuristic import is_deal_archived
from app.services.phone_service import extract_phones_from_entity_payload, normalize_phone


@dataclass
class DealCandidate:
    deal_id: int
    bitrix_deal_id: int
    title: str
    assigned_by_id: int | None
    assigned_name: str | None
    local_id: int


@dataclass
class MatchResult:
    match_status: str
    match_reason: str
    matched_contact_id: int | None = None
    matched_deal_id: int | None = None
    matched_deal_local_id: int | None = None
    matched_company_id: int | None = None
    candidates: list[DealCandidate] = field(default_factory=list)


@dataclass
class _MatcherIndexes:
    phone_index: dict[str, list[int]]
    company_phone_index: dict[str, int]
    contact_links: dict[int, list[int]]
    contact_company_ids: dict[int, int]
    deals_by_id: dict[int, CrmEntity]
    users: dict[int, str]


_INDEX_CACHE: dict[str, tuple[float, _MatcherIndexes]] = {}
_INDEX_CACHE_TTL_SECONDS = 60.0


def invalidate_matcher_cache(portal_id: str | None = None) -> None:
    if portal_id is None:
        _INDEX_CACHE.clear()
    else:
        _INDEX_CACHE.pop(portal_id, None)


class CallResultMatcher:
    def __init__(self, db: Session, portal_id: str):
        self.db = db
        self.portal_id = portal_id
        self._export_registry = ExportPhoneRegistry(db)
        self._phone_index: dict[str, list[int]] | None = None
        self._company_phone_index: dict[str, int] | None = None
        self._contact_links: dict[int, list[int]] | None = None
        self._contact_company_ids: dict[int, int] | None = None
        self._deals_by_id: dict[int, CrmEntity] | None = None
        self._users: dict[int, str] | None = None

    def build_indexes(self) -> None:
        cached = _INDEX_CACHE.get(self.portal_id)
        if cached is not None:
            built_at, indexes = cached
            if time.monotonic() - built_at < _INDEX_CACHE_TTL_SECONDS:
                self._apply_indexes(indexes)
                return

        self._phone_index = {}
        phones = self.db.scalars(
            select(CrmContactPhone).where(CrmContactPhone.portal_id == self.portal_id)
        )
        for p in phones:
            self._add_contact_phone(p.contact_id, p.value)

        contacts = list(
            self.db.scalars(
                select(CrmContact).where(CrmContact.portal_id == self.portal_id)
            )
        )
        self._contact_company_ids = {}
        for c in contacts:
            if c.company_id:
                self._contact_company_ids[c.contact_id] = int(c.company_id)
            if c.primary_phone:
                self._add_contact_phone(c.contact_id, c.primary_phone)
            for val, _ in extract_phones_from_entity_payload(c.raw_payload or {}):
                self._add_contact_phone(c.contact_id, val)

        self._company_phone_index = {}
        companies = self.db.scalars(
            select(CrmEntity).where(
                CrmEntity.portal_id == self.portal_id,
                CrmEntity.entity_type_id == ENTITY_COMPANY,
                CrmEntity.is_deleted.is_(False),
            )
        )
        for co in companies:
            raw = co.raw_payload or {}
            for val, _ in extract_phones_from_entity_payload(raw):
                norm = normalize_phone(val)
                if norm:
                    self._company_phone_index[norm] = co.entity_id

        deals = list(
            self.db.scalars(
                select(CrmEntity).where(
                    CrmEntity.portal_id == self.portal_id,
                    CrmEntity.entity_type_id == ENTITY_DEAL,
                    CrmEntity.is_deleted.is_(False),
                )
            )
        )
        self._deals_by_id = {d.entity_id: d for d in deals}
        for deal in deals:
            _ = deal.entity_id, deal.id, deal.title, deal.assigned_by_id, deal.raw_payload, deal.stage_id
            self.db.expunge(deal)

        self._contact_links = {}
        links = self.db.scalars(
            select(CrmContactLink).where(
                CrmContactLink.portal_id == self.portal_id,
                CrmContactLink.parent_entity_type_id == ENTITY_DEAL,
            )
        )
        for link in links:
            if link.parent_entity_id in self._deals_by_id:
                self._contact_links.setdefault(link.contact_id, []).append(link.parent_entity_id)

        self._users = {}
        for u in self.db.scalars(select(CrmUser).where(CrmUser.portal_id == self.portal_id)):
            if u.display_name:
                self._users[u.external_id] = u.display_name

        assert self._phone_index is not None
        assert self._company_phone_index is not None
        assert self._contact_links is not None
        assert self._contact_company_ids is not None
        assert self._deals_by_id is not None
        assert self._users is not None
        _INDEX_CACHE[self.portal_id] = (
            time.monotonic(),
            _MatcherIndexes(
                phone_index=self._phone_index,
                company_phone_index=self._company_phone_index,
                contact_links=self._contact_links,
                contact_company_ids=self._contact_company_ids,
                deals_by_id=self._deals_by_id,
                users=self._users,
            ),
        )

    def _add_contact_phone(self, contact_id: int, raw_value: str) -> None:
        assert self._phone_index is not None
        norm = normalize_phone(raw_value)
        if not norm:
            return
        ids = self._phone_index.setdefault(norm, [])
        if contact_id not in ids:
            ids.append(contact_id)

    def _apply_indexes(self, indexes: _MatcherIndexes) -> None:
        self._phone_index = indexes.phone_index
        self._company_phone_index = indexes.company_phone_index
        self._contact_links = indexes.contact_links
        self._contact_company_ids = indexes.contact_company_ids
        self._deals_by_id = indexes.deals_by_id
        self._users = indexes.users

    def _deals_for_contact(self, contact_id: int) -> list[CrmEntity]:
        assert self._contact_links is not None and self._deals_by_id is not None
        return [
            self._deals_by_id[eid]
            for eid in self._contact_links.get(contact_id, [])
            if eid in self._deals_by_id
        ]

    def match_row(
        self,
        normalized_phone: str | None,
        file_deal_id: int | None = None,
        is_valid_phone: bool = True,
    ) -> MatchResult:
        if not is_valid_phone or not normalized_phone:
            return MatchResult(
                match_status="invalid",
                match_reason="Некорректный телефон",
            )

        normalized_phone = normalize_phone(normalized_phone) or normalized_phone

        assert self._deals_by_id is not None
        assert self._phone_index is not None
        assert self._company_phone_index is not None
        assert self._contact_links is not None

        file_deal: CrmEntity | None = None
        if file_deal_id:
            file_deal = self._deals_by_id.get(file_deal_id)

        contact_ids = self._phone_index.get(normalized_phone, [])
        company_id = self._company_phone_index.get(normalized_phone)

        if file_deal is not None:
            if contact_ids:
                linked_deals = set()
                for cid in contact_ids:
                    linked_deals.update(self._contact_links.get(cid, []))
                if linked_deals and file_deal.entity_id not in linked_deals:
                    return MatchResult(
                        match_status="conflict",
                        match_reason="Конфликт deal_id и телефона",
                        matched_contact_id=contact_ids[0] if contact_ids else None,
                        matched_deal_id=file_deal.entity_id,
                        matched_deal_local_id=file_deal.id,
                        candidates=self._candidates_from_deals([file_deal]),
                    )
            return MatchResult(
                match_status="matched",
                match_reason="Сопоставлено по deal_id из файла",
                matched_deal_id=file_deal.entity_id,
                matched_deal_local_id=file_deal.id,
                matched_contact_id=contact_ids[0] if contact_ids else None,
            )

        export_match = self._match_by_export_registry(normalized_phone)
        if export_match is not None:
            return export_match

        if contact_ids:
            return self._match_by_contacts(contact_ids)

        if company_id:
            return self._match_by_company(company_id)

        return MatchResult(
            match_status="not_found",
            match_reason="Телефон не найден",
        )

    def _match_by_export_registry(self, normalized_phone: str) -> MatchResult | None:
        entries = self._export_registry.lookup(self.portal_id, normalized_phone)
        if not entries:
            return None

        assert self._deals_by_id is not None

        deal_ids = {entry.deal_id for entry in entries}
        if len(deal_ids) > 1:
            deals = [self._deals_by_id[did] for did in deal_ids if did in self._deals_by_id]
            if len(deals) > 1:
                chosen = self._pick_deal_by_max_id(deals)
                contact_id = next(
                    (e.contact_id for e in entries if e.deal_id == chosen.entity_id),
                    entries[0].contact_id,
                )
                return self._match_from_deals(
                    deals,
                    base_reason="Несколько сделок в реестре выгрузки",
                    matched_contact_id=contact_id,
                )

        entry = entries[0]
        deal = self._deals_by_id.get(entry.deal_id)
        return MatchResult(
            match_status="matched",
            match_reason="Сопоставлено по реестру выгрузки",
            matched_contact_id=entry.contact_id,
            matched_deal_id=entry.deal_id,
            matched_deal_local_id=deal.id if deal else None,
        )

    def _active_deals(self, deals: list[CrmEntity]) -> list[CrmEntity]:
        return [d for d in deals if not is_deal_archived(d)]

    def _candidates_from_deals(self, deals: list[CrmEntity]) -> list[DealCandidate]:
        assert self._users is not None
        return [
            DealCandidate(
                deal_id=d.entity_id,
                bitrix_deal_id=d.entity_id,
                title=d.title or "",
                assigned_by_id=d.assigned_by_id,
                assigned_name=self._users.get(d.assigned_by_id) if d.assigned_by_id else None,
                local_id=d.id,
            )
            for d in deals
        ]

    @staticmethod
    def _pick_deal_by_max_id(deals: list[CrmEntity]) -> CrmEntity:
        return max(deals, key=lambda d: d.entity_id)

    def _match_from_deals(
        self,
        deals: list[CrmEntity],
        *,
        base_reason: str,
        matched_contact_id: int | None = None,
        matched_company_id: int | None = None,
    ) -> MatchResult:
        chosen = self._pick_deal_by_max_id(deals)
        return MatchResult(
            match_status="matched",
            match_reason=f"{base_reason} — выбрана сделка с наибольшим номером (#{chosen.entity_id})",
            matched_contact_id=matched_contact_id,
            matched_deal_id=chosen.entity_id,
            matched_deal_local_id=chosen.id,
            matched_company_id=matched_company_id,
            candidates=self._candidates_from_deals(deals),
        )

    def _contact_for_deal(self, contact_ids: list[int], deal_id: int) -> int | None:
        assert self._contact_links is not None
        for cid in contact_ids:
            if deal_id in self._contact_links.get(cid, []):
                return cid
        return contact_ids[0] if contact_ids else None

    def _match_by_contacts(self, contact_ids: list[int]) -> MatchResult:
        assert self._contact_links is not None
        if len(contact_ids) > 1:
            per_contact_deals: list[set[int]] = []
            for cid in contact_ids:
                deals = {d.entity_id for d in self._active_deals(self._deals_for_contact(cid))}
                per_contact_deals.append(deals)
            all_unique = set().union(*per_contact_deals) if per_contact_deals else set()
            if len(all_unique) > 1:
                active_deals = [
                    self._deals_by_id[eid]
                    for eid in all_unique
                    if self._deals_by_id and eid in self._deals_by_id
                ]
                chosen = self._pick_deal_by_max_id(active_deals)
                return self._match_from_deals(
                    active_deals,
                    base_reason="Несколько контактов с одним телефоном — разные сделки",
                    matched_contact_id=self._contact_for_deal(contact_ids, chosen.entity_id),
                )

        all_deals: list[CrmEntity] = []
        for cid in contact_ids:
            all_deals.extend(self._deals_for_contact(cid))
        active = self._active_deals(all_deals)
        unique = {d.entity_id: d for d in active}
        active = list(unique.values())

        primary_contact = contact_ids[0] if len(contact_ids) == 1 else None

        if len(active) == 1:
            d = active[0]
            return MatchResult(
                match_status="matched",
                match_reason="Сопоставлено по телефону контакта",
                matched_contact_id=primary_contact or contact_ids[0],
                matched_deal_id=d.entity_id,
                matched_deal_local_id=d.id,
            )
        if len(active) > 1:
            return self._match_from_deals(
                active,
                base_reason="Несколько сделок",
                matched_contact_id=primary_contact or contact_ids[0],
            )

        matched_contact_id = primary_contact or (contact_ids[0] if contact_ids else None)
        unique_all = {d.entity_id: d for d in all_deals}
        if len(unique_all) == 1:
            d = next(iter(unique_all.values()))
            return MatchResult(
                match_status="matched",
                match_reason="Сопоставлено по телефону (закрытая сделка)",
                matched_contact_id=matched_contact_id,
                matched_deal_id=d.entity_id,
                matched_deal_local_id=d.id,
            )

        company_id = self._contact_company_id(contact_ids)
        if company_id:
            company_match = self._match_by_company(
                company_id,
                matched_contact_id=matched_contact_id,
            )
            if company_match.match_status != "not_found":
                return company_match

        return MatchResult(
            match_status="not_found",
            match_reason="Контакт найден, сделка не найдена",
            matched_contact_id=matched_contact_id,
            matched_company_id=company_id,
        )

    def _contact_company_id(self, contact_ids: list[int]) -> int | None:
        assert self._contact_company_ids is not None
        for cid in contact_ids:
            company_id = self._contact_company_ids.get(cid)
            if company_id:
                return company_id
        return None

    def _match_by_company(
        self,
        company_id: int,
        *,
        matched_contact_id: int | None = None,
    ) -> MatchResult:
        assert self._deals_by_id is not None
        all_company_deals = [
            d for d in self._deals_by_id.values() if self._deal_company_id(d) == company_id
        ]
        active = self._active_deals(all_company_deals)
        if len(active) == 1:
            d = active[0]
            return MatchResult(
                match_status="matched",
                match_reason="Сопоставлено по телефону компании",
                matched_contact_id=matched_contact_id,
                matched_company_id=company_id,
                matched_deal_id=d.entity_id,
                matched_deal_local_id=d.id,
            )
        if len(active) > 1:
            return self._match_from_deals(
                active,
                base_reason="Несколько сделок по компании",
                matched_contact_id=matched_contact_id,
                matched_company_id=company_id,
            )

        archived_unique = {d.entity_id: d for d in all_company_deals if is_deal_archived(d)}
        if len(archived_unique) == 1:
            d = next(iter(archived_unique.values()))
            return MatchResult(
                match_status="matched",
                match_reason="Сопоставлено по телефону компании (закрытая сделка)",
                matched_contact_id=matched_contact_id,
                matched_company_id=company_id,
                matched_deal_id=d.entity_id,
                matched_deal_local_id=d.id,
            )

        return MatchResult(
            match_status="not_found",
            match_reason="Компания найдена, сделка не найдена",
            matched_contact_id=matched_contact_id,
            matched_company_id=company_id,
        )

    @staticmethod
    def _deal_company_id(deal: CrmEntity) -> int | None:
        raw = deal.raw_payload or {}
        val = raw.get("companyId") or raw.get("COMPANY_ID")
        try:
            cid = int(val)
            return cid if cid > 0 else None
        except (TypeError, ValueError):
            return None

    def get_deal(self, bitrix_deal_id: int) -> CrmEntity | None:
        assert self._deals_by_id is not None
        return self._deals_by_id.get(bitrix_deal_id)

    def get_user_name(self, user_id: int | None) -> str | None:
        if user_id is None or self._users is None:
            return None
        return self._users.get(user_id)
