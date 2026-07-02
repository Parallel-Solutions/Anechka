"""LPR-based replacement contact search for manual review / hangup flows."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import ENTITY_DEAL
from app.repositories.crm_repository import CrmRepository
from app.services.intelligent_export.contact_lpr_classifier import build_lpr_classifier
from app.services.intelligent_export.contact_phone_heuristic import (
    ContactCandidate,
    collect_deal_contacts,
    pick_contact_for_deal,
    pick_phone_for_contact,
)
from app.services.lpr_service import load_lpr_config
from app.services.phone_service import normalize_phone


class LprContactSearchError(Exception):
    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message)
        self.detail = detail or message


class LprContactSearchProvider:
    """Find another deal contact using export-time LPR heuristics."""

    def __init__(self, db: Session, portal_id: str, settings: Settings):
        self.db = db
        self.portal_id = portal_id
        self.settings = settings
        self._lpr_config = load_lpr_config(db)
        self._classifier = build_lpr_classifier(
            settings,
            self._lpr_config,
            use_llm=bool(settings.llm_call_results_enabled),
        )

    def find_candidate(
        self,
        *,
        deal_id: int,
        exclude_phone: str | None,
        deal_contact_ids: list[int],
        exclude_contact_id: int | None = None,
    ) -> dict[str, Any] | None:
        result = self.find_replacement_contact(
            deal_id=deal_id,
            exclude_phone=exclude_phone,
            exclude_contact_id=exclude_contact_id,
        )
        if result is None:
            return None
        return {
            "contact_id": result["contact_id"],
            "phone": result.get("phone"),
            "confidence": result.get("confidence"),
            "reason": result.get("reason"),
        }

    def find_replacement_contact(
        self,
        *,
        deal_id: int,
        exclude_phone: str | None,
        exclude_contact_id: int | None = None,
    ) -> dict[str, Any] | None:
        crm_repo = CrmRepository(self.db, self.portal_id)
        deal = crm_repo.get_entity(ENTITY_DEAL, deal_id)
        if deal is None:
            raise LprContactSearchError(
                "Сделка не найдена в локальной CRM",
                detail="deal_not_found",
            )

        candidates = collect_deal_contacts(
            self.db,
            self.portal_id,
            deal,
            include_company_contacts=False,
        )
        if not candidates:
            raise LprContactSearchError(
                "Не удалось найти другой контакт по правилам ЛПР",
                detail="нет контактов сделки",
            )

        exclude_norm = normalize_phone(exclude_phone) if exclude_phone else None
        filtered = []
        for candidate in candidates:
            if exclude_contact_id and candidate.contact_id == exclude_contact_id:
                continue
            phone = pick_phone_for_contact(self.db, self.portal_id, candidate.contact_id)
            phone_norm = normalize_phone(phone) if phone else None
            if exclude_norm and phone_norm and phone_norm == exclude_norm:
                continue
            filtered.append(candidate)

        if not filtered:
            raise LprContactSearchError(
                "Не удалось найти другой контакт по правилам ЛПР",
                detail="все телефоны совпадают с исходным",
            )

        chosen, reason, confidence = pick_contact_for_deal(
            filtered,
            lpr_config=self._lpr_config,
            classifier=self._classifier,
            deal_title=deal.title or "",
        )
        if chosen is None:
            raise LprContactSearchError(
                "Не удалось найти другой контакт по правилам ЛПР",
                detail="нет подходящего ЛПР",
            )

        phone = pick_phone_for_contact(self.db, self.portal_id, chosen.contact_id)
        phone_norm = normalize_phone(phone) if phone else None
        if not phone_norm:
            raise LprContactSearchError(
                "Не удалось найти другой контакт по правилам ЛПР",
                detail="нет телефона у контакта",
            )
        if exclude_norm and phone_norm == exclude_norm:
            raise LprContactSearchError(
                "Не удалось найти другой контакт по правилам ЛПР",
                detail="все телефоны совпадают с исходным",
            )

        return {
            "contact_id": chosen.contact_id,
            "phone": phone_norm,
            "reason": reason,
            "confidence": confidence,
            "contact_name": chosen.contact.full_name or chosen.contact.name,
        }

    def find_by_keywords(
        self,
        *,
        deal_id: int,
        keywords: list[str],
        exclude_phone: str | None,
        exclude_contact_id: int | None = None,
    ) -> dict[str, Any] | None:
        if not keywords:
            return None

        crm_repo = CrmRepository(self.db, self.portal_id)
        deal = crm_repo.get_entity(ENTITY_DEAL, deal_id)
        if deal is None:
            raise LprContactSearchError(
                "Сделка не найдена в локальной CRM",
                detail="deal_not_found",
            )

        candidates = collect_deal_contacts(
            self.db,
            self.portal_id,
            deal,
            include_company_contacts=False,
        )
        if not candidates:
            return None

        exclude_norm = normalize_phone(exclude_phone) if exclude_phone else None
        filtered: list[ContactCandidate] = []
        for candidate in candidates:
            if exclude_contact_id and candidate.contact_id == exclude_contact_id:
                continue
            phone = pick_phone_for_contact(self.db, self.portal_id, candidate.contact_id)
            phone_norm = normalize_phone(phone) if phone else None
            if exclude_norm and phone_norm and phone_norm == exclude_norm:
                continue
            if not phone_norm:
                continue
            filtered.append(candidate)

        if not filtered:
            return None

        normalized_keywords = [k.strip().lower() for k in keywords if k and k.strip()]
        if not normalized_keywords:
            return None

        scored: list[tuple[int, ContactCandidate, str]] = []
        for candidate in filtered:
            text = candidate.searchable_text()
            matched = [kw for kw in normalized_keywords if kw in text]
            if matched:
                scored.append((len(matched), candidate, ", ".join(matched)))

        if not scored:
            return None

        scored.sort(key=lambda item: (-item[0], -item[1].sort_key()[0]))
        best_score, chosen, matched_kw = scored[0]
        phone = pick_phone_for_contact(self.db, self.portal_id, chosen.contact_id)
        phone_norm = normalize_phone(phone) if phone else None
        if not phone_norm:
            return None

        return {
            "contact_id": chosen.contact_id,
            "phone": phone_norm,
            "reason": f"Поиск по ключевым словам: {matched_kw}",
            "confidence": min(1.0, best_score / max(len(normalized_keywords), 1)),
            "contact_name": chosen.contact.full_name or chosen.contact.name,
        }


def build_contact_search_provider(
    db: Session,
    portal_id: str,
    settings: Settings,
):
    provider_name = (settings.contact_search_provider or "fake").strip().lower()
    if provider_name == "lpr":
        return LprContactSearchProvider(db, portal_id, settings)
    from app.services.call_results.contact_search_gateway import FakeContactSearchProvider

    return FakeContactSearchProvider()
