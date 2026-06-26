"""Deterministic post-processing of LLM-generated ExportPlan 2.0 candidates.

Fixes common contact-field mapping mistakes before validation, without calling the LLM.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from app.models import ENTITY_CONTACT, ENTITY_DEAL, ENTITY_LEAD
from app.services.export_plan.catalog import FieldCatalog
from app.services.intelligent_export.contact_phone_heuristic import (
    TOMORU_DEFAULT_CATEGORY_ID,
    TOMORU_REGION_FIELD,
)
from app.services.intelligent_export.tomoru_regions import (
    ORENBURG_REGION_SENTINEL,
    REGION_TITLE_STOPWORDS,
    is_tomoru_region_field,
    resolve_title_region_from_message,
    resolve_tomoru_region_from_message,
    try_parse_region_filter_value,
)
from app.services.intelligent_export.tomoru_stages import (
    KpStageCatalog,
    extract_years_from_text,
    normalize_homoglyphs,
    normalize_stage_name,
    parse_stage_mentions,
)

_LEGACY_RELATION_MAP = {
    "deal_contact": "deal_contact_link",
    "lead_contact": "lead_contact_link",
}

_NAME_HEADER_RE = re.compile(r"(имя|фио|контакт|фамилия|name|fio)", re.I)
_POST_HEADER_RE = re.compile(r"(должност|post|position)", re.I)
_DESC_HEADER_RE = re.compile(r"(описан|comment|коммент)", re.I)
_PHONE_HEADER_RE = re.compile(r"(телефон|phone)", re.I)

_STRICT_VALIDATION_TOKENS = re.compile(
    r"(без\s+пуст|обязательн|только\s+с\s+телефон|only\s+with\s+phone|not\s+empty)",
    re.I,
)

_TOMORU_TOKENS = re.compile(
    r"(tomoru|туморо|тумороу|обзвон|для\s+tomoru|номера\s+для)",
    re.I,
)

_FIO_CODES = ("LAST_NAME", "NAME", "SECOND_NAME")

def _field_ref(entity_type_id: int, field_code: str, source_alias: str) -> dict[str, Any]:
    return {
        "kind": "field",
        "field": {
            "entity_type_id": entity_type_id,
            "field_code": field_code,
            "source_alias": source_alias,
        },
    }


def _has_field(catalog: FieldCatalog, entity_type_id: int, field_code: str) -> bool:
    return catalog.get(entity_type_id, field_code) is not None


def _fio_value(entity_type_id: int, alias: str, catalog: FieldCatalog) -> dict[str, Any]:
    if not _has_field(catalog, entity_type_id, "LAST_NAME"):
        return _field_ref(entity_type_id, "TITLE", alias)
    parts = [
        _field_ref(entity_type_id, code, alias)
        for code in _FIO_CODES
        if _has_field(catalog, entity_type_id, code)
    ]
    concat_part: dict[str, Any] = {"kind": "concat", "parts": parts, "separator": " "}
    if _has_field(catalog, entity_type_id, "TITLE"):
        return {"kind": "coalesce", "parts": [concat_part, _field_ref(entity_type_id, "TITLE", alias)]}
    return concat_part


def _ensure_phone_transform(col: dict[str, Any]) -> None:
    transforms = col.setdefault("transforms", [])
    if not any(t.get("op") == "phone_normalize" for t in transforms):
        transforms.append({"op": "phone_normalize", "params": {}})


def _column_entity(col: dict[str, Any]) -> tuple[int | None, str | None]:
    value = col.get("value") or {}
    if value.get("kind") != "field":
        return None, None
    field = value.get("field") or {}
    return field.get("entity_type_id"), field.get("source_alias")


def _enrich_contact_column(col: dict[str, Any], catalog: FieldCatalog) -> None:
    entity_type_id, alias = _column_entity(col)
    if entity_type_id != ENTITY_CONTACT or alias is None:
        return
    header = (col.get("header") or "").lower()
    value = col.get("value") or {}
    if value.get("kind") != "field":
        return
    field = value.get("field") or {}
    field_code = (field.get("field_code") or "").upper()

    if _NAME_HEADER_RE.search(header):
        col["value"] = _fio_value(entity_type_id, alias, catalog)
        return

    if _POST_HEADER_RE.search(header) and _has_field(catalog, entity_type_id, "POST"):
        field["field_code"] = "POST"
        return

    if _DESC_HEADER_RE.search(header) and _has_field(catalog, entity_type_id, "COMMENTS"):
        field["field_code"] = "COMMENTS"
        return

    if _PHONE_HEADER_RE.search(header) or field_code in ("PHONE", "FM"):
        field["field_code"] = "PHONE"
        _ensure_phone_transform(col)


def _upgrade_relations(datasets: list[dict[str, Any]]) -> None:
    for dataset in datasets:
        refs = dataset.get("relation_refs") or []
        for ref in refs:
            code = ref.get("relation_code")
            if code in _LEGACY_RELATION_MAP:
                ref["relation_code"] = _LEGACY_RELATION_MAP[code]


def _strip_lenient_validation(sheet: dict[str, Any], *, strict: bool) -> None:
    if strict:
        return
    rules = sheet.get("validation_rules") or []
    if not rules:
        return
    phone_name_ids = set()
    for col in sheet.get("columns") or []:
        header = (col.get("header") or "").lower()
        cid = col.get("id")
        if cid and (_PHONE_HEADER_RE.search(header) or _NAME_HEADER_RE.search(header)):
            phone_name_ids.add(cid)
    if not phone_name_ids:
        return
    sheet["validation_rules"] = [
        r
        for r in rules
        if not (
            r.get("type") in ("required", "not_empty_after_transform")
            and r.get("column_id") in phone_name_ids
        )
    ]


_DATE_FIELD_CODES = frozenset({"DATE_CREATE", "DATE_MODIFY", "UPDATED_TIME", "CREATED_TIME"})


def _is_tomoru_request(user_message: str, plan: dict[str, Any]) -> bool:
    text = normalize_homoglyphs(
        " ".join(
            [
                user_message or "",
                plan.get("title") or "",
                plan.get("description") or "",
            ]
        )
    )
    return bool(_TOMORU_TOKENS.search(text))


def _has_closed_filter(dataset: dict[str, Any], deal_alias: str) -> bool:
    for filt in dataset.get("filters") or []:
        field = filt.get("field") or {}
        if field.get("entity_type_id") != ENTITY_DEAL:
            continue
        if (field.get("field_code") or "").upper() != "CLOSED":
            continue
        if field.get("source_alias") not in (None, deal_alias):
            continue
        if filt.get("op") == "eq" and str(filt.get("value", "")).upper() == "N":
            return True
    return False


def _ensure_not_archived_filter(dataset: dict[str, Any], deal_alias: str, catalog: FieldCatalog) -> None:
    if not _has_field(catalog, ENTITY_DEAL, "CLOSED"):
        return
    if _has_closed_filter(dataset, deal_alias):
        return
    dataset.setdefault("filters", []).append(
        {
            "field": {
                "entity_type_id": ENTITY_DEAL,
                "field_code": "CLOSED",
                "source_alias": deal_alias,
            },
            "op": "eq",
            "value": "N",
        }
    )


def _has_category_filter(dataset: dict[str, Any], deal_alias: str, category_id: int) -> bool:
    for filt in dataset.get("filters") or []:
        field = filt.get("field") or {}
        if field.get("entity_type_id") != ENTITY_DEAL:
            continue
        if (field.get("field_code") or "").upper() != "CATEGORY_ID":
            continue
        if field.get("source_alias") not in (None, deal_alias):
            continue
        if filt.get("op") == "eq" and filt.get("value") == category_id:
            return True
    return False


def _strip_category_filters(dataset: dict[str, Any], deal_alias: str) -> None:
    filters = dataset.get("filters") or []
    dataset["filters"] = [
        filt
        for filt in filters
        if not (
            (filt.get("field") or {}).get("entity_type_id") == ENTITY_DEAL
            and ((filt.get("field") or {}).get("field_code") or "").upper() == "CATEGORY_ID"
            and (filt.get("field") or {}).get("source_alias") in (None, deal_alias)
        )
    ]


def _ensure_commercial_proposal_filter(
    dataset: dict[str, Any],
    deal_alias: str,
    catalog: FieldCatalog,
    *,
    category_id: int = TOMORU_DEFAULT_CATEGORY_ID,
) -> None:
    if not _has_field(catalog, ENTITY_DEAL, "CATEGORY_ID"):
        return
    _strip_category_filters(dataset, deal_alias)
    if _has_category_filter(dataset, deal_alias, category_id):
        return
    dataset.setdefault("filters", []).append(
        {
            "field": {
                "entity_type_id": ENTITY_DEAL,
                "field_code": "CATEGORY_ID",
                "source_alias": deal_alias,
            },
            "op": "eq",
            "value": category_id,
        }
    )


def _strip_all_tomoru_region_filters(dataset: dict[str, Any], deal_alias: str) -> None:
    """Remove all UF iblock-49 region filters (e.g. wrong LLM values like Moscow for Orenburg)."""
    filters = dataset.get("filters") or []
    dataset["filters"] = [
        filt
        for filt in filters
        if not (
            (filt.get("field") or {}).get("entity_type_id") == ENTITY_DEAL
            and is_tomoru_region_field((filt.get("field") or {}).get("field_code"))
            and (filt.get("field") or {}).get("source_alias") in (None, deal_alias)
        )
    ]


def _strip_invalid_region_filters(dataset: dict[str, Any], deal_alias: str) -> None:
    """Drop UF region filters with placeholder or unparseable values."""
    filters = dataset.get("filters") or []
    kept: list[dict[str, Any]] = []
    for filt in filters:
        field = filt.get("field") or {}
        if field.get("entity_type_id") != ENTITY_DEAL:
            kept.append(filt)
            continue
        if not is_tomoru_region_field(field.get("field_code")):
            kept.append(filt)
            continue
        if field.get("source_alias") not in (None, deal_alias):
            kept.append(filt)
            continue
        if filt.get("op") != "eq":
            kept.append(filt)
            continue
        if try_parse_region_filter_value(filt.get("value")) is not None:
            kept.append(filt)
    dataset["filters"] = kept


def _strip_title_region_filters(dataset: dict[str, Any], deal_alias: str) -> None:
    filters = dataset.get("filters") or []
    kept: list[dict[str, Any]] = []
    for filt in filters:
        field = filt.get("field") or {}
        if field.get("entity_type_id") != ENTITY_DEAL:
            kept.append(filt)
            continue
        if (field.get("field_code") or "").upper() != "TITLE":
            kept.append(filt)
            continue
        if field.get("source_alias") not in (None, deal_alias):
            kept.append(filt)
            continue
        if filt.get("op") != "contains":
            kept.append(filt)
            continue
        val = str(filt.get("value") or "").lower()
        if any(stop in val or val in stop for stop in REGION_TITLE_STOPWORDS):
            continue
        kept.append(filt)
    dataset["filters"] = kept


def _has_region_filter(dataset: dict[str, Any], deal_alias: str, region_field: str, region_id: int) -> bool:
    for filt in dataset.get("filters") or []:
        field = filt.get("field") or {}
        if field.get("entity_type_id") != ENTITY_DEAL:
            continue
        if (field.get("field_code") or "").upper() != region_field.upper():
            continue
        if field.get("source_alias") not in (None, deal_alias):
            continue
        if filt.get("op") == "eq" and filt.get("value") == region_id:
            return True
    return False


def _strip_stage_filters(dataset: dict[str, Any], deal_alias: str) -> None:
    filters = dataset.get("filters") or []
    dataset["filters"] = [
        filt
        for filt in filters
        if not (
            (filt.get("field") or {}).get("entity_type_id") == ENTITY_DEAL
            and ((filt.get("field") or {}).get("field_code") or "").upper() == "STAGE_ID"
            and (filt.get("field") or {}).get("source_alias") in (None, deal_alias)
        )
    ]


def _strip_title_stage_filters(
    dataset: dict[str, Any],
    deal_alias: str,
    *,
    stage_names: frozenset[str],
) -> None:
    if not stage_names:
        return
    filters = dataset.get("filters") or []
    kept: list[dict[str, Any]] = []
    for filt in filters:
        field = filt.get("field") or {}
        if field.get("entity_type_id") != ENTITY_DEAL:
            kept.append(filt)
            continue
        if (field.get("field_code") or "").upper() != "TITLE":
            kept.append(filt)
            continue
        if field.get("source_alias") not in (None, deal_alias):
            kept.append(filt)
            continue
        if filt.get("op") != "contains":
            kept.append(filt)
            continue
        val = normalize_stage_name(str(filt.get("value") or ""))
        if not val:
            kept.append(filt)
            continue
        if any(val == name or val in name or name in val for name in stage_names):
            continue
        kept.append(filt)
    dataset["filters"] = kept


def _deal_alias_from_dataset(dataset: dict[str, Any]) -> str | None:
    sources = dataset.get("sources") or []
    deal_source = next((s for s in sources if s.get("entity_type_id") == ENTITY_DEAL), None)
    if deal_source is None:
        return None
    return deal_source.get("alias") or "deal"


def _ensure_stage_filter(
    dataset: dict[str, Any],
    deal_alias: str,
    catalog: FieldCatalog,
    *,
    user_message: str,
    kp_stages: KpStageCatalog | None,
    enrichment_warnings: list[str] | None,
    preferred_category_id: int | None = None,
) -> None:
    if kp_stages is None or not _has_field(catalog, ENTITY_DEAL, "STAGE_ID"):
        return
    mentions = parse_stage_mentions(user_message)
    if not mentions:
        return

    resolved_ids, unresolved_names = kp_stages.resolve_many(
        mentions,
        preferred_category_id=preferred_category_id,
    )
    if unresolved_names and enrichment_warnings is not None:
        names = ", ".join(f"«{name}»" for name in unresolved_names)
        enrichment_warnings.append(
            f"Не удалось определить стадию: {names}; в фильтр включены только распознанные стадии."
        )
    if not resolved_ids:
        return

    _strip_stage_filters(dataset, deal_alias)
    _strip_title_stage_filters(dataset, deal_alias, stage_names=kp_stages.all_normalized_names())

    stage_field = {
        "entity_type_id": ENTITY_DEAL,
        "field_code": "STAGE_ID",
        "source_alias": deal_alias,
    }
    if len(resolved_ids) == 1:
        dataset.setdefault("filters", []).append(
            {"field": stage_field, "op": "eq", "value": resolved_ids[0]}
        )
    else:
        dataset.setdefault("filters", []).append(
            {"field": stage_field, "op": "in", "values": resolved_ids}
        )


def _filter_value_year(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        try:
            return int(text[:4])
        except ValueError:
            return None
    return None


def _strip_spurious_year_date_filters(
    dataset: dict[str, Any],
    deal_alias: str,
    *,
    years: set[int],
) -> None:
    """Drop DATE_CREATE ranges that LLM inferred from a year in the stage name."""
    if not years:
        return
    filters = dataset.get("filters") or []
    kept: list[dict] = []
    for filt in filters:
        field = filt.get("field") or {}
        if field.get("entity_type_id") != ENTITY_DEAL:
            kept.append(filt)
            continue
        code = (field.get("field_code") or "").upper()
        if code not in _DATE_FIELD_CODES:
            kept.append(filt)
            continue
        if field.get("source_alias") not in (None, deal_alias):
            kept.append(filt)
            continue
        year = _filter_value_year(filt.get("value"))
        if year is not None and year in years and filt.get("op") in ("gte", "lte", "gt", "lt", "eq"):
            continue
        kept.append(filt)
    dataset["filters"] = kept


def _years_from_stage_mentions(user_message: str) -> set[int]:
    years: set[int] = set()
    for mention in parse_stage_mentions(user_message):
        years.update(extract_years_from_text(mention))
    return years


def _ensure_region_filter(
    dataset: dict[str, Any],
    deal_alias: str,
    catalog: FieldCatalog,
    *,
    user_message: str,
) -> None:
    title_region = resolve_title_region_from_message(user_message)
    iblock_region = resolve_tomoru_region_from_message(user_message)
    if title_region is None and iblock_region is None:
        return

    _strip_all_tomoru_region_filters(dataset, deal_alias)
    _strip_invalid_region_filters(dataset, deal_alias)
    _strip_title_region_filters(dataset, deal_alias)

    if title_region is not None:
        if not _has_field(catalog, ENTITY_DEAL, TOMORU_REGION_FIELD):
            return
        dataset.setdefault("filters", []).append(
            {
                "field": {
                    "entity_type_id": ENTITY_DEAL,
                    "field_code": TOMORU_REGION_FIELD,
                    "source_alias": deal_alias,
                },
                "op": "eq",
                "value": ORENBURG_REGION_SENTINEL,
            }
        )
        return

    region_id, _label, _aliases = iblock_region
    if not _has_field(catalog, ENTITY_DEAL, TOMORU_REGION_FIELD):
        return
    if _has_region_filter(dataset, deal_alias, TOMORU_REGION_FIELD, region_id):
        return
    dataset.setdefault("filters", []).append(
        {
            "field": {
                "entity_type_id": ENTITY_DEAL,
                "field_code": TOMORU_REGION_FIELD,
                "source_alias": deal_alias,
            },
            "op": "eq",
            "value": region_id,
        }
    )


def _apply_tomoru_mode(
    plan: dict[str, Any],
    catalog: FieldCatalog,
    *,
    user_message: str,
) -> None:
    """Convert plan to deals-only Tomoru export with server-side contact heuristic."""
    datasets = plan.get("datasets") or []
    if not datasets:
        return

    dataset = datasets[0]
    deal_alias = "deal"
    sources = dataset.get("sources") or []
    deal_source = next((s for s in sources if s.get("entity_type_id") == ENTITY_DEAL), None)
    if deal_source:
        deal_alias = deal_source.get("alias") or deal_alias

    dataset["sources"] = [{"alias": deal_alias, "entity_type_id": ENTITY_DEAL}]
    dataset["primary_entity_type_id"] = ENTITY_DEAL
    dataset["relation_refs"] = []
    _ensure_not_archived_filter(dataset, deal_alias, catalog)
    _ensure_commercial_proposal_filter(dataset, deal_alias, catalog)
    _ensure_region_filter(dataset, deal_alias, catalog, user_message=user_message)

    workbook = plan.setdefault("workbook", {})
    workbook["include_errors_sheet"] = False
    workbook["include_params_sheet"] = False

    sheets = workbook.get("sheets") or []
    if not sheets:
        return
    sheet = sheets[0]
    sheet["columns"] = [
        {
            "id": "phone",
            "header": "Телефон",
            "value": {"kind": "constant", "value": ""},
            "transforms": [{"op": "phone_digits_only", "params": {}}],
        }
    ]
    sheet["post_process"] = {
        "op": "tomoru_phones",
        "deal_alias": deal_alias,
        "include_company_contacts": True,
        "include_company_phones": True,
        "fetch_company_contacts_live": True,
        "deduplicate_phones": True,
        "exclude_archived": True,
        "use_llm_for_lpr": True,
        "category_id": TOMORU_DEFAULT_CATEGORY_ID,
    }
    sheet["validation_rules"] = []


def enrich_plan(
    plan_dict: dict[str, Any],
    *,
    user_message: str,
    catalog: FieldCatalog,
    kp_stages: KpStageCatalog | None = None,
    enrichment_warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Return a copy of *plan_dict* with contact mapping and relation fixes applied."""
    plan = copy.deepcopy(plan_dict)
    user_message = normalize_homoglyphs(user_message or "")
    strict_validation = bool(_STRICT_VALIDATION_TOKENS.search(user_message))

    is_tomoru = _is_tomoru_request(user_message, plan)
    if is_tomoru:
        _apply_tomoru_mode(
            plan,
            catalog,
            user_message=user_message,
        )

    datasets = plan.get("datasets") or []
    stage_years = _years_from_stage_mentions(user_message)
    if kp_stages is not None and parse_stage_mentions(user_message):
        preferred_category_id = TOMORU_DEFAULT_CATEGORY_ID if is_tomoru else None
        for dataset in datasets:
            deal_alias = _deal_alias_from_dataset(dataset)
            if deal_alias is None:
                continue
            _ensure_stage_filter(
                dataset,
                deal_alias,
                catalog,
                user_message=user_message,
                kp_stages=kp_stages,
                enrichment_warnings=enrichment_warnings,
                preferred_category_id=preferred_category_id,
            )
            if stage_years:
                _strip_spurious_year_date_filters(dataset, deal_alias, years=stage_years)

    _upgrade_relations(datasets)

    workbook = plan.get("workbook") or {}
    for sheet in workbook.get("sheets") or []:
        if sheet.get("mode") != "rows":
            continue
        for col in sheet.get("columns") or []:
            _enrich_contact_column(col, catalog)
        _strip_lenient_validation(sheet, strict=strict_validation)

    return plan


def is_tomoru_plan(plan: dict[str, Any]) -> bool:
    for sheet in (plan.get("workbook") or {}).get("sheets") or []:
        post_process = sheet.get("post_process") or {}
        if post_process.get("op") == "tomoru_phones":
            return True
    return False


def _strip_arbitrary_date_create_year_ranges(dataset: dict[str, Any], deal_alias: str) -> None:
    """Drop full-calendar-year DATE_CREATE ranges often inferred from stage names."""
    filters = dataset.get("filters") or []
    years_with_range: set[int] = set()
    for filt in filters:
        field = filt.get("field") or {}
        if field.get("entity_type_id") != ENTITY_DEAL:
            continue
        code = (field.get("field_code") or "").upper()
        if code not in _DATE_FIELD_CODES:
            continue
        if field.get("source_alias") not in (None, deal_alias):
            continue
        year = _filter_value_year(filt.get("value"))
        if year is None:
            continue
        val = str(filt.get("value") or "")
        if filt.get("op") == "gte" and val.endswith("-01-01"):
            years_with_range.add(year)
        if filt.get("op") == "lte" and val.endswith("-12-31"):
            years_with_range.add(year)

    if not years_with_range:
        return

    kept: list[dict] = []
    for filt in filters:
        field = filt.get("field") or {}
        if field.get("entity_type_id") != ENTITY_DEAL:
            kept.append(filt)
            continue
        code = (field.get("field_code") or "").upper()
        if code not in _DATE_FIELD_CODES:
            kept.append(filt)
            continue
        if field.get("source_alias") not in (None, deal_alias):
            kept.append(filt)
            continue
        year = _filter_value_year(filt.get("value"))
        if year is not None and year in years_with_range and filt.get("op") in ("gte", "lte", "gt", "lt", "eq"):
            continue
        kept.append(filt)
    dataset["filters"] = kept


def sanitize_tomoru_plan(plan_dict: dict[str, Any]) -> dict[str, Any]:
    """Remove spurious DATE_CREATE filters from Tomoru export plans."""
    plan = copy.deepcopy(plan_dict)
    if not is_tomoru_plan(plan):
        return plan
    for dataset in plan.get("datasets") or []:
        deal_alias = _deal_alias_from_dataset(dataset)
        if deal_alias is None:
            continue
        years: set[int] = set()
        years |= _years_from_stage_mentions(plan.get("title") or "")
        years |= _years_from_stage_mentions(plan.get("description") or "")
        for filt in dataset.get("filters") or []:
            field = filt.get("field") or {}
            if (field.get("field_code") or "").upper() != "STAGE_ID":
                continue
            val = filt.get("value")
            if val is not None:
                years |= set(extract_years_from_text(str(val)))
            for item in filt.get("values") or []:
                years |= set(extract_years_from_text(str(item)))
        if years:
            _strip_spurious_year_date_filters(dataset, deal_alias, years=years)
        else:
            _strip_arbitrary_date_create_year_ranges(dataset, deal_alias)
    return plan
