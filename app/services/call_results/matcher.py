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
from app.services.intelligent_export.contact_phone_heuristic import is_deal_archived
from app.services.phone_service import extract_phones_from_multifield, normalize_phone


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
        self._phone_index: dict[str, list[int]] | None = None
        self._company_phone_index: dict[str, int] | None = None
        self._contact_links: dict[int, list[int]] | None = None
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
            norm = normalize_phone(p.value)
            if norm:
                self._phone_index.setdefault(norm, []).append(p.contact_id)

        contacts = self.db.scalars(
            select(CrmContact).where(
                CrmContact.portal_id == self.portal_id,
                CrmContact.primary_phone.isnot(None),
            )
        )
        for c in contacts:
            norm = normalize_phone(c.primary_phone or "")
            if norm:
                ids = self._phone_index.setdefault(norm, [])
                if c.contact_id not in ids:
                    ids.append(c.contact_id)

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
            for val, _ in extract_phones_from_multifield(raw.get("PHONE") or raw.get("phone")):
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
        assert self._deals_by_id is not None
        assert self._users is not None
        _INDEX_CACHE[self.portal_id] = (
            time.monotonic(),
            _MatcherIndexes(
                phone_index=self._phone_index,
                company_phone_index=self._company_phone_index,
                contact_links=self._contact_links,
                deals_by_id=self._deals_by_id,
                users=self._users,
            ),
        )

    def _apply_indexes(self, indexes: _MatcherIndexes) -> None:
        self._phone_index = indexes.phone_index
        self._company_phone_index = indexes.company_phone_index
        self._contact_links = indexes.contact_links
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

        if contact_ids:
            return self._match_by_contacts(contact_ids)

        if company_id:
            return self._match_by_company(company_id)

        return MatchResult(
            match_status="not_found",
            match_reason="Телефон не найден",
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
                return MatchResult(
                    match_status="ambiguous",
                    match_reason="Несколько контактов с одним телефоном — разные сделки",
                    candidates=self._candidates_from_deals(active_deals),
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
            return MatchResult(
                match_status="ambiguous",
                match_reason="Несколько сделок",
                matched_contact_id=primary_contact,
                candidates=self._candidates_from_deals(active),
            )
        return MatchResult(
            match_status="not_found",
            match_reason="Контакт найден, сделка не найдена",
            matched_contact_id=primary_contact or (contact_ids[0] if contact_ids else None),
        )

    def _match_by_company(self, company_id: int) -> MatchResult:
        assert self._deals_by_id is not None
        deals = [
            d
            for d in self._deals_by_id.values()
            if self._deal_company_id(d) == company_id and not is_deal_archived(d)
        ]
        if len(deals) == 1:
            d = deals[0]
            return MatchResult(
                match_status="matched",
                match_reason="Сопоставлено по телефону компании",
                matched_company_id=company_id,
                matched_deal_id=d.entity_id,
                matched_deal_local_id=d.id,
            )
        if len(deals) > 1:
            return MatchResult(
                match_status="ambiguous",
                match_reason="Несколько сделок по компании",
                matched_company_id=company_id,
                candidates=self._candidates_from_deals(deals),
            )
        return MatchResult(
            match_status="not_found",
            match_reason="Компания найдена, сделка не найдена",
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
