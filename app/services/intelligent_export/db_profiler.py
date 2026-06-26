"""Read-only profiler of the imported portal data.

Builds a compact statistical snapshot of ``crm_entities.raw_payload`` (and the
field catalog) that the memory generator turns into planner "memory" hints. It
performs only ``SELECT`` queries and never mutates data. Computation is done in
Python over ORM rows so it stays portable across PostgreSQL (production) and the
SQLite in-memory database used in tests.

Sensitive values (phone/e-mail) are never stored: only aggregate counts and
shares (e.g. "share of phones starting with 8") leave this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CrmEntity, ENTITY_COMPANY, ENTITY_CONTACT, ENTITY_DEAL, ENTITY_LEAD
from app.services.export_plan.catalog import FieldCatalog

ENTITY_TYPE_IDS = (ENTITY_LEAD, ENTITY_DEAL, ENTITY_CONTACT, ENTITY_COMPANY)

# Multifield codes handled separately from the generic fill-rate scan.
MULTIFIELD_CODES = ("PHONE", "EMAIL", "FM")

# Known FK relations per entity type. Each entry maps a canonical label to the
# list of payload key spellings to probe (case-insensitive). Bitrix payloads use
# lower camelCase ("contactId"); some exports use UPPER snake ("CONTACT_ID").
FK_FIELDS: dict[int, dict[str, tuple[str, ...]]] = {
    ENTITY_DEAL: {
        "contactId": ("contactId", "CONTACT_ID"),
        "companyId": ("companyId", "COMPANY_ID"),
    },
    ENTITY_CONTACT: {
        "companyId": ("companyId", "COMPANY_ID"),
    },
    ENTITY_LEAD: {
        "contactId": ("contactId", "CONTACT_ID"),
        "companyId": ("companyId", "COMPANY_ID"),
    },
}

_DEFAULT_SAMPLE_CAP = 5000


@dataclass
class EntityProfile:
    entity_type_id: int
    total_seen: int = 0
    fill_counts: dict[str, int] = field(default_factory=dict)
    fill_rates: dict[str, float] = field(default_factory=dict)
    multifield: dict[str, dict] = field(default_factory=dict)
    fk_link_shares: dict[str, dict] = field(default_factory=dict)
    phone_format: dict | None = None


@dataclass
class PortalProfile:
    portal_id: str
    entities: dict[int, EntityProfile] = field(default_factory=dict)
    # (entity_type_id, field_code, display_name, has_informative_display)
    uf_fields: list[tuple[int, str, str, bool]] = field(default_factory=list)


def _payload_get(payload: dict, *keys: str):
    """Case-insensitive lookup that tries each key plus its UPPER/lower forms."""
    if not isinstance(payload, dict):
        return None
    for key in keys:
        for variant in (key, key.upper(), key.lower()):
            if variant in payload:
                return payload[variant]
    return None


def _is_empty(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


def _is_linked(value) -> bool:
    """A FK is "linked" when it is present, non-empty and not the zero sentinel."""
    if _is_empty(value):
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip() not in ("0", "")
    return True


def _multifield_value_strings(value) -> list[str]:
    """Extract the raw string values from a Bitrix multifield list.

    Accepts ``[{"VALUE": "8...", ...}]`` (UPPER export) or ``[{"value": ...}]``
    (camelCase) or a plain scalar/list of scalars.
    """
    out: list[str] = []
    items = value if isinstance(value, list) else [value]
    for item in items:
        if isinstance(item, dict):
            raw = item.get("VALUE", item.get("value"))
        else:
            raw = item
        if raw is not None and str(raw).strip() != "":
            out.append(str(raw))
    return out


class PortalProfiler:
    def __init__(self, db: Session, portal_id: str, catalog: FieldCatalog):
        self.db = db
        self.portal_id = portal_id
        self.catalog = catalog

    def profile(self, *, sample_cap: int = _DEFAULT_SAMPLE_CAP) -> PortalProfile:
        profile = PortalProfile(portal_id=self.portal_id)
        for entity_type_id in ENTITY_TYPE_IDS:
            profile.entities[entity_type_id] = self._profile_entity(entity_type_id, sample_cap)
        profile.uf_fields = self._collect_uf_fields()
        return profile

    def _scanned_codes(self, entity_type_id: int) -> list[str]:
        """Codes whose fill-rate is meaningful from ``raw_payload``.

        Only payload-backed (``storage == "jsonb"``) fields are scanned.
        Denormalized column-backed fields (TITLE, AMOUNT, CATEGORY_ID, ...) live
        in dedicated ``crm_entities`` columns, so their payload presence is
        unreliable and would yield false "sparse field" signals.
        """
        codes: list[str] = []
        for entry in self.catalog.for_entity(entity_type_id):
            code = entry.field_code.upper()
            if code in MULTIFIELD_CODES:
                continue
            if entry.storage != "jsonb":
                continue
            if code not in codes:
                codes.append(code)
        return codes

    def _profile_entity(self, entity_type_id: int, sample_cap: int) -> EntityProfile:
        prof = EntityProfile(entity_type_id=entity_type_id)
        codes = self._scanned_codes(entity_type_id)
        fill_counts = {code: 0 for code in codes}
        multifield_counts = {code: 0 for code in MULTIFIELD_CODES}
        multifield_present = {code: False for code in MULTIFIELD_CODES}
        fk_spec = FK_FIELDS.get(entity_type_id, {})
        fk_counts = {label: 0 for label in fk_spec}
        phone_total = 0
        phone_starts_8 = 0

        stmt = (
            select(CrmEntity)
            .where(
                CrmEntity.portal_id == self.portal_id,
                CrmEntity.entity_type_id == entity_type_id,
                CrmEntity.is_deleted.is_(False),
            )
            .limit(max(1, sample_cap))
        )

        total = 0
        for entity in self.db.scalars(stmt):
            total += 1
            payload = entity.raw_payload if isinstance(entity.raw_payload, dict) else {}

            for code in codes:
                if not _is_empty(_payload_get(payload, code)):
                    fill_counts[code] += 1

            for code in MULTIFIELD_CODES:
                value = _payload_get(payload, code)
                if value is not None:
                    multifield_present[code] = True
                if not _is_empty(value):
                    multifield_counts[code] += 1
                    if code == "PHONE":
                        for raw in _multifield_value_strings(value):
                            digits = re.sub(r"\D", "", raw)
                            if digits:
                                phone_total += 1
                                if digits[0] == "8":
                                    phone_starts_8 += 1

            for label, candidates in fk_spec.items():
                if _is_linked(_payload_get(payload, *candidates)):
                    fk_counts[label] += 1

        prof.total_seen = total
        prof.fill_counts = fill_counts
        prof.fill_rates = {
            code: (fill_counts[code] / total if total else 0.0) for code in codes
        }
        prof.multifield = {
            code: {
                "present": multifield_present[code],
                "count": multifield_counts[code],
                "fill_rate": (multifield_counts[code] / total if total else 0.0),
            }
            for code in MULTIFIELD_CODES
        }
        prof.fk_link_shares = {
            label: {
                "linked": fk_counts[label],
                "share": (fk_counts[label] / total if total else 0.0),
            }
            for label in fk_spec
        }
        if phone_total:
            prof.phone_format = {
                "sample": phone_total,
                "starts_with_8": phone_starts_8 / phone_total,
            }
        return prof

    def _collect_uf_fields(self) -> list[tuple[int, str, str, bool]]:
        out: list[tuple[int, str, str, bool]] = []
        for (entity_type_id, code), entry in sorted(self.catalog.fields.items()):
            if not entry.is_custom and not code.startswith("UF_"):
                continue
            if not code.startswith("UF_"):
                continue
            display = entry.display_name or ""
            informative = bool(display) and display.strip().upper() != code.upper()
            out.append((entity_type_id, code, display, informative))
        return out
