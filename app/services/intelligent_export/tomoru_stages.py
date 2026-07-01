"""Deal stage name → STAGE_ID resolution across all Bitrix funnels."""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ENTITY_DEAL, CrmDictionary, CrmDictionaryEntry
from app.services.intelligent_export.contact_phone_heuristic import TOMORU_DEFAULT_CATEGORY_ID
from app.services.intelligent_export.tomoru_regions import is_filter_placeholder

logger = logging.getLogger(__name__)

STAGE_CANONICAL_ALIASES: tuple[tuple[str, str], ...] = (
    ("новая", "C15:NEW"),
    ("теплый", "C15:4"),
    ("тёплый", "C15:4"),
)

_PUNCT_RE = re.compile(r"[^\w\s\-]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
_YO_RE = str.maketrans({"ё": "е", "Ё": "Е"})
_HOMOGLYPH_RE = str.maketrans(
    {
        "a": "а",
        "A": "А",
        "c": "с",
        "C": "С",
        "e": "е",
        "E": "Е",
        "o": "о",
        "O": "О",
        "p": "р",
        "P": "Р",
        "x": "х",
        "X": "Х",
        "y": "у",
        "Y": "У",
        "k": "к",
        "K": "К",
        "m": "м",
        "M": "М",
        "t": "т",
        "T": "Т",
    }
)
_STAGE_CODE_RE = re.compile(r"^(?:сделк(?:а|и|у|ой|е)|deal|stage)\s+(.+)$", re.I)
_BARE_STAGE_CODE_RE = re.compile(r"^(?:C\d+:[\w]+|\d+)$", re.I)

_STAGE_MENTION_RE = re.compile(
    r"(?:стад(?:ия|ии|ию|ией|ие|и)|на\s+стадии|stage)\s+(.+?)(?=(?:,\s*(?:стад|на\s+стадии|stage)\b)|$)",
    re.I | re.S,
)
_MENTION_SPLIT_RE = re.compile(r"\s+(?:и|and)\s+|\s*,\s*", re.I)


def normalize_stage_name(text: str) -> str:
    normalized = (text or "").strip().lower().translate(_YO_RE)
    normalized = _PUNCT_RE.sub(" ", normalized)
    normalized = _SPACE_RE.sub(" ", normalized).strip()
    return normalized


def normalize_homoglyphs(text: str) -> str:
    """Map Latin look-alikes to Cyrillic (e.g. Latin o in «туморoу»)."""
    return (text or "").translate(_HOMOGLYPH_RE)


def _clean_stage_mention(part: str) -> str:
    part = part.strip(" .")
    if not part:
        return part
    match = _STAGE_CODE_RE.match(part)
    if match:
        return match.group(1).strip()
    return part


def parse_stage_mentions(user_message: str) -> list[str]:
    """Extract individual stage labels from free text (may be multiple)."""
    text = user_message or ""
    if not text.strip():
        return []
    mentions: list[str] = []
    for match in _STAGE_MENTION_RE.finditer(text):
        chunk = match.group(1).strip()
        if not chunk:
            continue
        for part in _MENTION_SPLIT_RE.split(chunk):
            part = _clean_stage_mention(part)
            if part:
                mentions.append(part)
    return mentions


def extract_years_from_text(text: str) -> list[int]:
    """Return 4-digit years (19xx/20xx) mentioned in free text."""
    return [int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})\b", text or "")]


class KpStageCatalog:
    """In-memory index of deal stages from all funnels."""

    def __init__(self, stages: list[dict[str, Any]]) -> None:
        self._codes: set[str] = set()
        self._code_category: dict[str, int | None] = {}
        self._name_index: dict[str, str] = {}
        self._name_codes: dict[str, list[str]] = {}
        self._normalized_names: set[str] = set()

        ordered = sorted(
            stages,
            key=lambda s: (
                0 if s.get("category_id") != TOMORU_DEFAULT_CATEGORY_ID else 1,
                s.get("category_id") if s.get("category_id") is not None else -1,
            ),
        )
        for stage in ordered:
            self._register_stage(stage)

        for alias, target_code in STAGE_CANONICAL_ALIASES:
            if target_code in self._codes:
                norm_alias = normalize_stage_name(alias)
                if norm_alias:
                    self._name_index[norm_alias] = target_code
                    self._name_codes[norm_alias] = [target_code]
                    self._normalized_names.add(norm_alias)

    def _register_stage(self, stage: dict[str, Any]) -> None:
        code = str(stage.get("id") or stage.get("STATUS_ID") or "").strip()
        if not code:
            return
        name = str(stage.get("name") or stage.get("NAME") or "").strip()
        category_id = stage.get("category_id")
        self._codes.add(code)
        if category_id is not None:
            self._code_category[code] = int(category_id)

        if name:
            norm = normalize_stage_name(name)
            if norm:
                self._name_index[norm] = code
                codes = self._name_codes.setdefault(norm, [])
                if code not in codes:
                    codes.append(code)
                self._normalized_names.add(norm)

        norm_code = normalize_stage_name(code)
        if norm_code:
            self._name_index.setdefault(norm_code, code)
            codes = self._name_codes.setdefault(norm_code, [])
            if code not in codes:
                codes.append(code)

    @classmethod
    def from_stages(cls, stages: list[dict[str, Any]]) -> KpStageCatalog:
        return cls(stages)

    @classmethod
    def load(
        cls,
        db: Session,
        portal_id: str,
        *,
        bitrix_client: Any | None = None,
        category_id: int | None = None,
    ) -> KpStageCatalog:
        if category_id is not None:
            stages_db = _load_stages_from_db(db, portal_id, category_id)
            stages_api = _try_load_stages_from_bitrix(bitrix_client, category_id=category_id)
            stages = _merge_stages(stages_db, stages_api)
        else:
            stages_db = _load_all_stages_from_db(db, portal_id)
            stages_api = _try_load_stages_from_bitrix(bitrix_client)
            stages = _merge_stages(stages_db, stages_api)
        return cls(stages)

    def all_normalized_names(self) -> frozenset[str]:
        return frozenset(self._normalized_names)

    def has_code(self, code: str) -> bool:
        return str(code).strip() in self._codes

    def resolve(self, text: str, *, preferred_category_id: int | None = None) -> str | None:
        codes = self.resolve_codes(text, preferred_category_id=preferred_category_id)
        return codes[0] if len(codes) == 1 else None

    def resolve_codes(
        self,
        text: str,
        *,
        preferred_category_id: int | None = None,
    ) -> list[str]:
        direct = try_parse_stage_code(text, self)
        if direct is not None:
            return [direct]

        normalized = normalize_stage_name(text)
        if not normalized:
            return []

        if normalized in self._name_codes:
            return self._pick_codes(self._name_codes[normalized], preferred_category_id)

        matches = [
            code
            for name, codes in self._name_codes.items()
            if normalized in name or name in normalized
            for code in codes
        ]
        unique = list(dict.fromkeys(matches))
        return self._pick_codes(unique, preferred_category_id)

    def _pick_codes(
        self,
        codes: list[str],
        preferred_category_id: int | None,
    ) -> list[str]:
        if not codes:
            return []
        if preferred_category_id is None or len(codes) == 1:
            return codes
        preferred = [
            code
            for code in codes
            if self._code_category.get(code) == preferred_category_id
        ]
        if preferred:
            return preferred
        return codes

    def resolve_many(
        self,
        texts: list[str],
        *,
        preferred_category_id: int | None = None,
    ) -> tuple[list[str], list[str]]:
        resolved: list[str] = []
        unresolved: list[str] = []
        seen: set[str] = set()
        for text in texts:
            codes = self.resolve_codes(text, preferred_category_id=preferred_category_id)
            if not codes:
                unresolved.append(text)
                continue
            for code in codes:
                if code not in seen:
                    seen.add(code)
                    resolved.append(code)
        return resolved, unresolved


def try_parse_stage_code(value: Any, catalog: KpStageCatalog) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or is_filter_placeholder(text):
        return None
    if catalog.has_code(text):
        return text
    cleaned = _clean_stage_mention(text)
    if cleaned and cleaned != text:
        if catalog.has_code(cleaned):
            return cleaned
        text = cleaned
    if _BARE_STAGE_CODE_RE.match(text) and catalog.has_code(text):
        return text
    norm = normalize_stage_name(text)
    if norm in catalog._name_index:
        return catalog._name_index[norm]
    return None


def try_resolve_stage_id(
    text: str,
    catalog: KpStageCatalog,
    *,
    preferred_category_id: int | None = None,
) -> str | None:
    codes = catalog.resolve_codes(text, preferred_category_id=preferred_category_id)
    if len(codes) == 1:
        return codes[0]
    return None


def resolve_stage_id(text: str, catalog: KpStageCatalog) -> str:
    code = try_resolve_stage_id(text, catalog)
    if code is None:
        raise ValueError(text)
    return code


def _default_bitrix_client() -> Any | None:
    settings = get_settings()
    if not settings.bitrix_webhook_url:
        return None
    from app.services.bitrix_client import BitrixClient

    return BitrixClient(settings)


def _try_load_stages_from_bitrix(
    bitrix_client: Any | None,
    *,
    category_id: int | None = None,
) -> list[dict[str, Any]]:
    client = bitrix_client or _default_bitrix_client()
    if client is None:
        return []
    try:
        if category_id is not None:
            return _load_stages_from_bitrix(client, category_id)
        return _load_all_stages_from_bitrix(client)
    except Exception:
        logger.warning("Failed to load deal stages from Bitrix API", exc_info=True)
        return []


def _merge_stages(*sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for stages in sources:
        for stage in stages:
            code = str(stage.get("id") or stage.get("STATUS_ID") or "").strip()
            if not code:
                continue
            merged = dict(by_id.get(code, {}))
            merged.update(stage)
            merged["id"] = code
            by_id[code] = merged
    return list(by_id.values())


def _category_id_from_dictionary_code(dictionary_code: str) -> int | None:
    if dictionary_code == "status_DEAL_STAGE":
        return 0
    prefix = "status_DEAL_STAGE_"
    if dictionary_code.startswith(prefix):
        suffix = dictionary_code[len(prefix) :]
        try:
            return int(suffix)
        except ValueError:
            return None
    return None


def _load_all_stages_from_db(db: Session, portal_id: str) -> list[dict[str, Any]]:
    dictionaries = db.scalars(
        select(CrmDictionary).where(
            CrmDictionary.portal_id == portal_id,
            CrmDictionary.is_active.is_(True),
            CrmDictionary.entity_type_id == ENTITY_DEAL,
            CrmDictionary.dictionary_code.like("status_DEAL_STAGE%"),
        )
    ).all()
    stages: list[dict[str, Any]] = []
    seen: set[str] = set()
    for dictionary in dictionaries:
        category_id = _category_id_from_dictionary_code(dictionary.dictionary_code)
        for stage in _dictionary_stages(db, portal_id, dictionary.dictionary_code):
            code = str(stage.get("id") or "").strip()
            if not code or code in seen:
                continue
            seen.add(code)
            stages.append({**stage, "category_id": category_id})
    return stages


def _load_stages_from_db(db: Session, portal_id: str, category_id: int) -> list[dict[str, Any]]:
    stages: list[dict[str, Any]] = []
    dict_code = "status_DEAL_STAGE" if category_id == 0 else f"status_DEAL_STAGE_{category_id}"
    for stage in _dictionary_stages(db, portal_id, dict_code):
        stages.append({**stage, "category_id": category_id})
    if category_id != 0:
        for stage in _dictionary_stages(db, portal_id, "status_DEAL_STAGE"):
            code = str(stage.get("id") or "").strip()
            if code and not any(s.get("id") == code for s in stages):
                stages.append({**stage, "category_id": 0})
    return stages


def _dictionary_stages(db: Session, portal_id: str, dictionary_code: str) -> list[dict[str, Any]]:
    dictionary = db.scalar(
        select(CrmDictionary).where(
            CrmDictionary.portal_id == portal_id,
            CrmDictionary.dictionary_code == dictionary_code,
            CrmDictionary.is_active.is_(True),
        )
    )
    if dictionary is None:
        return []
    entries = db.scalars(
        select(CrmDictionaryEntry).where(
            CrmDictionaryEntry.dictionary_id == dictionary.id,
            CrmDictionaryEntry.is_active.is_(True),
        )
    )
    return [
        {"id": e.external_id, "name": e.raw_value or e.external_id}
        for e in entries
        if e.external_id
    ]


def build_stage_names_map(
    db: Session,
    portal_id: str,
    *,
    category_id: int | None = None,
) -> dict[str, str]:
    """Map STAGE_ID → display name from imported CRM dictionaries."""
    if category_id is not None:
        stages = _load_stages_from_db(db, portal_id, category_id)
    else:
        stages = _load_all_stages_from_db(db, portal_id)
    return {
        str(stage["id"]): str(stage.get("name") or stage["id"])
        for stage in stages
        if stage.get("id")
    }


def _load_all_stages_from_bitrix(client: Any) -> list[dict[str, Any]]:
    stages: list[dict[str, Any]] = []
    for category in client.get_categories():
        cat_id = int(category.get("id", 0))
        stages.extend(_load_stages_from_bitrix(client, cat_id))
    return stages


def _load_stages_from_bitrix(client: Any, category_id: int) -> list[dict[str, Any]]:
    return [
        {**stage, "category_id": category_id}
        for stage in client.get_stages(category_id)
        if stage.get("id")
    ]


def archive_stage_ids_from_stages(stages: list[dict[str, Any]]) -> frozenset[str]:
    """Return STATUS_ID values whose display name indicates archive."""
    out: set[str] = set()
    for stage in stages:
        name = normalize_stage_name(str(stage.get("name") or ""))
        if "архив" not in name:
            continue
        stage_id = str(stage.get("id") or "").strip()
        if stage_id:
            out.add(stage_id)
    return frozenset(out)


def resolve_archive_stage_ids(
    db: Session,
    portal_id: str,
    category_id: int,
    *,
    client: Any | None = None,
) -> frozenset[str]:
    stages = _load_stages_from_db(db, portal_id, category_id)
    if not stages and client is not None:
        try:
            stages = _load_stages_from_bitrix(client, category_id)
        except Exception:
            logger.warning(
                "Failed to load funnel %s stages from Bitrix for archive filter",
                category_id,
                exc_info=True,
            )
    return archive_stage_ids_from_stages(stages)
