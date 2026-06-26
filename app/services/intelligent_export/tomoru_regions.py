"""Tomoru region list (iblock 49) and helpers to resolve region filter values."""



from __future__ import annotations



import re

from dataclasses import dataclass

from typing import Any



from app.services.intelligent_export.contact_phone_heuristic import TOMORU_REGION_FIELD



# Legacy «Регион (КП)» UF on deals without iblock-49 value.

TOMORU_LEGACY_KP_REGION_FIELD = "UF_CRM_1559576107786"



# Sentinel for compiler OR-filter: title contains OR legacy UF match.

ORENBURG_REGION_SENTINEL = "__tomoru_region:orenburg__"



# Bitrix list element IDs (iblock 49) + title synonyms for legacy cards without UF region.

TOMORU_REGIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (

    (1107, "санкт-петербург", ("санкт-петербург", "санкт петербург", "петербург", "спб", "питер")),

    (1105, "москва", ("москва", "москов")),

    (1091, "томск", ("томск", "томская")),

    (1089, "тверск", ("тверск",)),

    (1007, "амурск", ("амурск",)),

)



# Regions without iblock-49 id — filtered via title + legacy UF (OR in compiler).

@dataclass(frozen=True)

class TomoruTitleRegion:

    key: str

    legacy_uf_id: int

    title_needle: str

    aliases: tuple[str, ...]





TOMORU_TITLE_REGIONS: tuple[TomoruTitleRegion, ...] = (

    TomoruTitleRegion(

        key="orenburg",

        legacy_uf_id=171,

        title_needle="Оренбург",

        aliases=("оренбург", "оренбурга", "оренбургская"),

    ),

)



REGION_TITLE_STOPWORDS = frozenset(

    {

        "санкт-петербург",

        "санкт петербург",

        "петербург",

        "спб",

        "питер",

        "москва",

        "москов",

        "томск",

        "томская",

        "тверск",

        "амурск",

        "оренбург",

        "оренбурга",

        "оренбургская",

        "область",

        "край",

    }

)



_PLACEHOLDER_RE = re.compile(r"^<(.+)>$")

_REGION_ID_PLACEHOLDER_RE = re.compile(r"(?:id\s+)?региона\s*(.+)", re.I)





def is_filter_placeholder(value: Any) -> bool:

    if not isinstance(value, str):

        return False

    text = value.strip()

    return text.startswith("<") and text.endswith(">")





def resolve_region_id_from_text(text: str) -> int | None:

    """Match region list element id from free text (user message, placeholder, label)."""

    normalized = (text or "").strip().lower()

    if not normalized:

        return None

    match = _PLACEHOLDER_RE.match(normalized)

    if match:

        normalized = match.group(1).strip()

        inner = _REGION_ID_PLACEHOLDER_RE.search(normalized)

        if inner:

            normalized = inner.group(1).strip()

    for region_id, _label, aliases in TOMORU_REGIONS:

        if any(alias in normalized for alias in aliases):

            return region_id

        if _label in normalized:

            return region_id

    return None





def resolve_title_region_from_message(user_message: str) -> TomoruTitleRegion | None:

    text = (user_message or "").lower()

    if not text.strip():

        return None

    for region in TOMORU_TITLE_REGIONS:

        if any(alias in text for alias in region.aliases):

            return region

    return None





def try_parse_region_filter_value(value: Any) -> int | None:

    """Return numeric region id when *value* is already valid or resolvable from text."""

    if value is None:

        return None

    if isinstance(value, bool):

        return None

    if isinstance(value, int):

        return value

    if isinstance(value, float) and value.is_integer():

        return int(value)

    text = str(value).strip()

    if not text:

        return None

    if text == ORENBURG_REGION_SENTINEL:

        return None

    if text.isdigit():

        return int(text)

    return resolve_region_id_from_text(text)


def is_valid_tomoru_region_filter_value(value: Any) -> bool:
    """True for iblock-49 numeric ids and title-only region sentinels accepted by the compiler."""
    if value == ORENBURG_REGION_SENTINEL:
        return True
    return try_parse_region_filter_value(value) is not None


def resolve_region_filter_value(value: Any) -> int:

    """Strict resolver for compiler — raises ValueError when id cannot be determined."""

    if value == ORENBURG_REGION_SENTINEL:

        raise ValueError(str(value))

    region_id = try_parse_region_filter_value(value)

    if region_id is None:

        raise ValueError(str(value))

    return region_id





def resolve_tomoru_region_from_message(user_message: str) -> tuple[int, str, tuple[str, ...]] | None:

    text = (user_message or "").lower()

    if not text.strip():

        return None

    for region_id, label, aliases in TOMORU_REGIONS:

        if any(alias in text for alias in aliases):

            return region_id, label, aliases

    return None





def is_tomoru_region_field(field_code: str | None) -> bool:

    return (field_code or "").upper() == TOMORU_REGION_FIELD.upper()





def is_orenburg_region_filter(filt: dict[str, Any], deal_alias: str) -> bool:

    field = filt.get("field") or {}

    if field.get("entity_type_id") != 2:

        return False

    if not is_tomoru_region_field(field.get("field_code")):

        return False

    if field.get("source_alias") not in (None, deal_alias):

        return False

    return filt.get("op") == "eq" and filt.get("value") == ORENBURG_REGION_SENTINEL


