"""Metadata catalog — allowed fields for ExportPlan validation.

The catalog is the *only* source of truth for which fields exist, how they are
stored (column vs whitelisted JSONB key), their normalized data type, allowed
filter operators and sensitivity. AI may only reference fields that appear
here, and every reference is re-checked server-side.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CrmFieldDefinition, CrmFieldSemantic, ENTITY_COMPANY, ENTITY_CONTACT, ENTITY_DEAL, ENTITY_LEAD

ENTITY_TYPE_IDS = (ENTITY_LEAD, ENTITY_DEAL, ENTITY_CONTACT, ENTITY_COMPANY)

# Denormalized CrmEntity columns exposed to ExportPlan (MVP).
DENORM_FIELD_MAP: dict[str, str] = {
    "TITLE": "title",
    "CATEGORY_ID": "category_id",
    "STAGE_ID": "stage_id",
    "ASSIGNED_BY_ID": "assigned_by_id",
    "OPPORTUNITY": "amount",
    "AMOUNT": "amount",
    "SOURCE_ID": "source_id",
    "CURRENCY_ID": "currency_id",
    "DATE_CREATE": "created_time",
    "DATE_MODIFY": "updated_time",
    "CLOSEDATE": "closed_at",
    "ID": "entity_id",
}

# Normalized metadata for the denormalized columns.
# code -> (data_type, sensitive, groupable, dictionary_code)
DENORM_META: dict[str, tuple[str, bool, bool, str | None]] = {
    "TITLE": ("string", False, False, None),
    "CATEGORY_ID": ("enumeration", False, True, None),
    "STAGE_ID": ("enumeration", False, True, None),
    "ASSIGNED_BY_ID": ("user", False, True, "users"),
    "OPPORTUNITY": ("number", False, False, None),
    "AMOUNT": ("number", False, False, None),
    "SOURCE_ID": ("enumeration", False, True, None),
    "CURRENCY_ID": ("enumeration", False, True, None),
    "DATE_CREATE": ("datetime", False, False, None),
    "DATE_MODIFY": ("datetime", False, False, None),
    "CLOSEDATE": ("date", False, False, None),
    "ID": ("integer", False, False, None),
}

# Well-known Bitrix multifields always available from raw_payload (sensitive).
KNOWN_MULTIFIELDS: dict[str, tuple[str, str]] = {
    # code -> (data_type, display)
    "PHONE": ("string", "Телефон"),
    "EMAIL": ("string", "E-mail"),
}

SENSITIVE_FIELD_CODES = frozenset({"PHONE", "EMAIL"})

_OPS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "string": ("eq", "ne", "contains", "starts_with", "in", "not_in", "is_null", "is_not_null"),
    "text": ("eq", "ne", "contains", "starts_with", "in", "not_in", "is_null", "is_not_null"),
    "free_text": ("eq", "ne", "contains", "starts_with", "is_null", "is_not_null"),
    "integer": ("eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "is_null", "is_not_null"),
    "number": ("eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "is_null", "is_not_null"),
    "decimal": ("eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "is_null", "is_not_null"),
    "date": ("eq", "ne", "gt", "gte", "lt", "lte", "is_null", "is_not_null"),
    "datetime": ("eq", "ne", "gt", "gte", "lt", "lte", "is_null", "is_not_null"),
    "enumeration": ("eq", "ne", "in", "not_in", "is_null", "is_not_null"),
    "status": ("eq", "ne", "in", "not_in", "is_null", "is_not_null"),
    "user": ("eq", "ne", "in", "not_in", "is_null", "is_not_null"),
    "boolean": ("eq", "ne", "is_null", "is_not_null"),
}

_DEFAULT_OPS = ("eq", "ne", "in", "not_in", "is_null", "is_not_null")


def normalize_data_type(field_type: str | None, data_category: str | None) -> str:
    ft = (field_type or "").lower()
    dc = (data_category or "").lower()
    if "date" in ft or dc == "date":
        return "datetime" if "time" in ft else "date"
    if ft in ("integer", "int"):
        return "integer"
    if ft in ("double", "money", "float", "decimal") or dc == "financial":
        return "number"
    if ft in ("boolean", "bool"):
        return "boolean"
    if ft in ("enumeration", "crm_status", "iblock_element", "crm_category") or dc in ("status", "classification"):
        return "enumeration"
    if ft in ("user", "employee") or dc == "relation":
        return "user"
    return "string"


@dataclass
class FieldCatalogEntry:
    entity_type_id: int
    field_code: str
    display_name: str
    field_type: str
    is_custom: bool
    is_multiple: bool
    storage: str  # "column" | "jsonb"
    column_name: str | None = None
    filterable: bool = True
    sortable: bool = True
    exportable: bool = True
    data_type: str = "string"
    groupable: bool = False
    sensitive: bool = False
    dictionary_code: str | None = None
    description: str | None = None
    allowed_filter_ops: tuple[str, ...] = _DEFAULT_OPS

    def descriptor(self) -> dict:
        return {
            "entity_type_id": self.entity_type_id,
            "field_code": self.field_code,
            "display_name": self.display_name,
            "description": self.description,
            "data_type": self.data_type,
            "storage": self.storage,
            "selectable": self.exportable,
            "filterable": self.filterable,
            "sortable": self.sortable,
            "groupable": self.groupable,
            "sensitive": self.sensitive,
            "is_multiple": self.is_multiple,
            "allowed_filter_ops": list(self.allowed_filter_ops),
            "dictionary_code": self.dictionary_code,
        }


@dataclass
class FieldCatalog:
    portal_id: str
    fields: dict[tuple[int, str], FieldCatalogEntry] = field(default_factory=dict)
    denied_fields: frozenset[tuple[int, str]] = field(default_factory=frozenset)

    def get(self, entity_type_id: int, field_code: str) -> FieldCatalogEntry | None:
        return self.fields.get((entity_type_id, field_code.upper()))

    def is_allowed(self, entity_type_id: int, field_code: str) -> bool:
        key = (entity_type_id, field_code.upper())
        if key in self.denied_fields:
            return False
        return key in self.fields

    def for_entity(self, entity_type_id: int) -> list[FieldCatalogEntry]:
        return [e for (etid, _), e in self.fields.items() if etid == entity_type_id]

    def search(self, query: str, *, entity_type_id: int | None = None, limit: int = 20) -> list[FieldCatalogEntry]:
        """Deterministic search by code, display name and description.

        Ranking: exact code match > code prefix > display-name substring >
        description substring. No vector DB required for MVP.
        """
        q = (query or "").strip().lower()
        if not q:
            return []
        scored: list[tuple[int, FieldCatalogEntry]] = []
        for (etid, code), entry in self.fields.items():
            if entity_type_id is not None and etid != entity_type_id:
                continue
            if (etid, code) in self.denied_fields:
                continue
            name = (entry.display_name or "").lower()
            desc = (entry.description or "").lower()
            code_l = code.lower()
            score = 0
            if code_l == q or name == q:
                score = 100
            elif code_l.startswith(q) or name.startswith(q):
                score = 80
            elif q in code_l or q in name:
                score = 60
            elif q in desc:
                score = 40
            if score:
                scored.append((score, entry))
        scored.sort(key=lambda t: (-t[0], t[1].display_name))
        return [e for _, e in scored[:limit]]

    def snapshot_hash(self) -> str:
        items = sorted(
            (e.entity_type_id, e.field_code, e.storage, e.data_type, e.sensitive)
            for e in self.fields.values()
        )
        payload = json.dumps([list(i) for i in items], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def descriptor(self, *, include_sensitive: bool = True) -> list[dict]:
        out = []
        for (etid, code), entry in sorted(self.fields.items()):
            if entry.sensitive and not include_sensitive:
                continue
            if (etid, code) in self.denied_fields:
                continue
            out.append(entry.descriptor())
        return out

    @classmethod
    def load(
        cls,
        db: Session,
        portal_id: str,
        *,
        denied_field_codes: frozenset[tuple[int, str]] | None = None,
    ) -> FieldCatalog:
        catalog = cls(portal_id=portal_id, denied_fields=denied_field_codes or frozenset())
        for entity_type_id in ENTITY_TYPE_IDS:
            for code, column in DENORM_FIELD_MAP.items():
                data_type, sensitive, groupable, dict_code = DENORM_META.get(code, ("string", False, False, None))
                catalog.fields[(entity_type_id, code)] = FieldCatalogEntry(
                    entity_type_id=entity_type_id,
                    field_code=code,
                    display_name=code,
                    field_type="system",
                    is_custom=False,
                    is_multiple=False,
                    storage="column",
                    column_name=column,
                    data_type=data_type,
                    groupable=groupable,
                    sensitive=sensitive,
                    dictionary_code=dict_code,
                    sortable=True,
                    allowed_filter_ops=_OPS_BY_TYPE.get(data_type, _DEFAULT_OPS),
                )
            for code, (data_type, display) in KNOWN_MULTIFIELDS.items():
                catalog.fields[(entity_type_id, code)] = FieldCatalogEntry(
                    entity_type_id=entity_type_id,
                    field_code=code,
                    display_name=display,
                    field_type="multifield",
                    is_custom=False,
                    is_multiple=True,
                    storage="jsonb",
                    column_name=None,
                    data_type=data_type,
                    groupable=False,
                    sensitive=True,
                    sortable=False,
                    allowed_filter_ops=_OPS_BY_TYPE.get(data_type, _DEFAULT_OPS),
                )

        rows = db.execute(
            select(CrmFieldDefinition, CrmFieldSemantic)
            .outerjoin(CrmFieldSemantic, CrmFieldSemantic.field_definition_id == CrmFieldDefinition.id)
            .where(
                CrmFieldDefinition.portal_id == portal_id,
                CrmFieldDefinition.is_active.is_(True),
            )
        ).all()

        for field_def, semantic in rows:
            code = (field_def.original_field_name or field_def.upper_name or "").upper()
            if not code:
                continue
            if code in DENORM_FIELD_MAP:
                entry = catalog.fields.get((field_def.entity_type_id, code))
                if entry and semantic and semantic.display_name:
                    entry.display_name = semantic.display_name
                continue
            display = (
                (semantic.display_name if semantic else None)
                or field_def.title
                or field_def.list_label
                or code
            )
            data_category = semantic.data_category if semantic else None
            data_type = normalize_data_type(field_def.field_type, data_category)
            sensitive = bool(
                (data_category in ("contact",) if data_category else False) or code in SENSITIVE_FIELD_CODES
            )
            dict_code = None
            if semantic and semantic.is_dictionary:
                dict_code = f"enum_{code}"
            catalog.fields[(field_def.entity_type_id, code)] = FieldCatalogEntry(
                entity_type_id=field_def.entity_type_id,
                field_code=code,
                display_name=display,
                field_type=field_def.field_type or "string",
                is_custom=field_def.is_custom,
                is_multiple=field_def.is_multiple,
                storage="jsonb",
                column_name=None,
                data_type=data_type,
                groupable=data_type in ("enumeration", "status", "user", "boolean"),
                sensitive=sensitive,
                dictionary_code=dict_code,
                description=(semantic.short_description if semantic else None),
                sortable=not field_def.is_multiple,
                filterable=True,
                allowed_filter_ops=_OPS_BY_TYPE.get(data_type, _DEFAULT_OPS),
            )
        return catalog
