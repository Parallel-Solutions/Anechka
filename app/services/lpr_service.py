"""Эвристическое определение ЛПР (лиц, принимающих решения).

Список ключевых слов и сканируемых полей хранится в app_settings и
редактируется пользователем на странице настроек. Если значения не заданы,
используются значения по умолчанию (POST + UF-должность + ФИО-поля;
отраслевой список ключевых слов — см. lpr_keywords_industry).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models import AppSetting, utcnow
from app.services.bitrix_import.contact_parser import POST_CUSTOM_FIELD
from app.services.lpr_keywords_industry import INDUSTRY_LPR_KEYWORDS

logger = logging.getLogger(__name__)

LPR_KEYWORDS_KEY = "lpr_keywords"
LPR_FIELDS_KEY = "lpr_fields"
LPR_STOPWORDS_KEY = "lpr_stopwords"

GENERIC_LPR_KEYWORDS: list[str] = [
    "директор",
    "ген. директор",
    "генеральный директор",
    "гендиректор",
    "исполнительный директор",
    "коммерческий директор",
    "финансовый директор",
    "технический директор",
    "руководитель",
    "начальник",
    "владелец",
    "собственник",
    "учредитель",
    "управляющий",
    "заведующий",
    "президент",
    "главный",
    "глава",
    "основатель",
    "индивидуальный предприниматель",
    "founder",
    "co-founder",
    "ceo",
    "owner",
    "director",
    "head",
    "chief",
    "president",
]


def _merge_keyword_lists(*groups: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for group in groups:
        for item in group:
            key = item.lower()
            if not item or key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def _merge_field_lists(*groups: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for group in groups:
        for item in group:
            text = str(item).strip()
            if not text:
                continue
            key = text.upper()
            if key in seen:
                continue
            seen.add(key)
            merged.append(text)
    return merged


# Отраслевые фразы выше по списку = выше приоритет при выборе ЛПР; generic — fallback в конце.
DEFAULT_LPR_KEYWORDS: list[str] = _merge_keyword_lists(INDUSTRY_LPR_KEYWORDS, GENERIC_LPR_KEYWORDS)

# Поля контакта для keyword-сканирования (Bitrix UPPER codes).
DEFAULT_LPR_FIELDS: list[str] = [
    "POST",
    POST_CUSTOM_FIELD,
    "LAST_NAME",
    "SECOND_NAME",
]

DEFAULT_LPR_STOPWORDS: list[str] = [
    "не работает",
    "бывш",
    "уволен",
    "уволил",
    "в декрете",
    "декрет",
    "не актуал",
    "устарел",
    "не звонить",
    "ошибочно",
]

FIELD_TITLES: dict[str, str] = {
    "POST": "Должность",
    "NAME": "Имя",
    "LAST_NAME": "Фамилия",
    "SECOND_NAME": "Отчество",
    "COMMENTS": "Комментарий",
    POST_CUSTOM_FIELD: "Должность (UF)",
    "post": "Должность",
    "post_custom": "Должность (UF)",
    "last_name": "Фамилия",
    "second_name": "Отчество",
    "comments": "Комментарий",
}

# Configured Bitrix field code -> dict keys to read (first non-empty wins).
FIELD_LOOKUP_ALIASES: dict[str, tuple[str, ...]] = {
    "POST": ("POST", "post"),
    POST_CUSTOM_FIELD: (POST_CUSTOM_FIELD, "post_custom"),
    "LAST_NAME": ("LAST_NAME", "last_name", "lastName"),
    "SECOND_NAME": ("SECOND_NAME", "second_name", "secondName"),
    "NAME": ("NAME", "name"),
    "COMMENTS": ("COMMENTS", "comments"),
}


@dataclass
class LprConfig:
    keywords: list[str]
    fields: list[str]
    stopwords: list[str] = field(default_factory=list)


def _normalize_list(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    items: list[str] = []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return list(default)
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                items = [str(x).strip() for x in parsed]
            else:
                items = [text]
        except json.JSONDecodeError:
            items = [line.strip() for line in text.replace(",", "\n").splitlines()]
    elif isinstance(value, (list, tuple)):
        items = [str(x).strip() for x in value]
    cleaned = [x for x in items if x]
    return cleaned or list(default)


def _get_setting(db: Session, key: str) -> str | None:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return row.value if row else None


def load_lpr_config(db: Session) -> LprConfig:
    stored_keywords = _normalize_list(_get_setting(db, LPR_KEYWORDS_KEY), [])
    stored_fields = _normalize_list(_get_setting(db, LPR_FIELDS_KEY), [])
    stored_stopwords = _normalize_list(_get_setting(db, LPR_STOPWORDS_KEY), [])
    return LprConfig(
        keywords=_merge_keyword_lists(DEFAULT_LPR_KEYWORDS, stored_keywords),
        fields=_merge_field_lists(DEFAULT_LPR_FIELDS, stored_fields),
        stopwords=_merge_keyword_lists(DEFAULT_LPR_STOPWORDS, stored_stopwords),
    )


def save_lpr_config(
    db: Session,
    keywords: list[str],
    fields: list[str],
    stopwords: list[str],
) -> LprConfig:
    clean_keywords = _merge_keyword_lists(
        DEFAULT_LPR_KEYWORDS,
        _normalize_list(keywords, []),
    )
    clean_fields = _merge_field_lists(
        DEFAULT_LPR_FIELDS,
        _normalize_list(fields, []),
    )
    clean_stopwords = _merge_keyword_lists(
        DEFAULT_LPR_STOPWORDS,
        _normalize_list(stopwords, []),
    )
    for key, value in (
        (LPR_KEYWORDS_KEY, clean_keywords),
        (LPR_FIELDS_KEY, clean_fields),
        (LPR_STOPWORDS_KEY, clean_stopwords),
    ):
        payload = json.dumps(value, ensure_ascii=False)
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        if row:
            row.value = payload
            row.updated_at = utcnow()
        else:
            db.add(AppSetting(key=key, value=payload))
    db.commit()
    return LprConfig(keywords=clean_keywords, fields=clean_fields, stopwords=clean_stopwords)


def _resolve_field_keys(field: str) -> tuple[str, ...]:
    if field in FIELD_LOOKUP_ALIASES:
        return FIELD_LOOKUP_ALIASES[field]
    upper = field.upper()
    if upper in FIELD_LOOKUP_ALIASES:
        return FIELD_LOOKUP_ALIASES[upper]
    lower = field.lower()
    if lower in FIELD_LOOKUP_ALIASES:
        return FIELD_LOOKUP_ALIASES[lower]
    if field != lower:
        return (field, lower)
    return (field,)


def _extract_field_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, (list, tuple)):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                parts.append(str(item.get("VALUE", "") or item.get("value", "")))
            else:
                parts.append(str(item))
        return " ".join(p for p in parts if p)
    return str(raw)


def _field_text(contact: dict[str, Any], field: str) -> str:
    for key in _resolve_field_keys(field):
        text = _extract_field_text(contact.get(key))
        if text:
            return text
    return ""


def _set_if_missing(target: dict[str, Any], key: str, value: Any) -> None:
    if value in (None, "", []):
        return
    if key not in target or target.get(key) in (None, "", []):
        target[key] = value


def contact_to_lpr_dict(contact: Any) -> dict[str, Any]:
    """Словарь с каноническими ключами Bitrix для detect_lpr()."""
    from app.models import CrmContact

    if isinstance(contact, CrmContact):
        raw = dict(contact.raw_payload or {})
        _set_if_missing(raw, "POST", contact.post)
        if contact.post_custom:
            raw[POST_CUSTOM_FIELD] = contact.post_custom
            _set_if_missing(raw, "POST", contact.post_custom)
        _set_if_missing(raw, "LAST_NAME", contact.last_name)
        _set_if_missing(raw, "NAME", contact.name)
        _set_if_missing(raw, "SECOND_NAME", contact.second_name)
        raw.setdefault("ID", contact.contact_id)
        return raw

    data = dict(contact)
    post = data.get("POST") or data.get("post")
    post_custom = data.get(POST_CUSTOM_FIELD) or data.get("post_custom")
    _set_if_missing(data, "POST", post)
    if post_custom:
        data[POST_CUSTOM_FIELD] = post_custom
        _set_if_missing(data, "POST", post_custom)
    _set_if_missing(data, "LAST_NAME", data.get("LAST_NAME") or data.get("last_name"))
    _set_if_missing(data, "SECOND_NAME", data.get("SECOND_NAME") or data.get("second_name"))
    _set_if_missing(data, "NAME", data.get("NAME") or data.get("name"))
    _set_if_missing(data, "COMMENTS", data.get("COMMENTS") or data.get("comments"))
    return data


def lpr_keyword_rank(contact: dict[str, Any], config: LprConfig) -> tuple[int | None, str]:
    """Индекс ключевого слова с наивысшим приоритетом (меньше = выше) или (None, "").

    Порядок config.keywords задаёт приоритет: чем выше строка в списке, тем
    приоритетнее совпадение. Среди всех сканируемых полей выбирается лучшее
    совпадение по минимальному индексу ключевого слова.
    """
    payload = contact_to_lpr_dict(contact)
    field_texts = [(fld, _field_text(payload, fld)) for fld in config.fields]

    lowered_stop = [sw.lower() for sw in config.stopwords if sw]
    for _fld, text in field_texts:
        if not text:
            continue
        haystack = text.lower()
        if any(sw in haystack for sw in lowered_stop):
            return None, ""

    lowered_keywords = [(kw, kw.lower()) for kw in config.keywords if kw]
    best_rank: int | None = None
    best_reason = ""
    for fld, text in field_texts:
        if not text:
            continue
        haystack = text.lower()
        for rank, (original_kw, kw) in enumerate(lowered_keywords):
            if kw not in haystack:
                continue
            if best_rank is None or rank < best_rank:
                best_rank = rank
                title = FIELD_TITLES.get(fld, fld)
                best_reason = f"{title}: «{text.strip()}» (ключевое слово «{original_kw}»)"
    return best_rank, best_reason


def detect_lpr(contact: dict[str, Any], config: LprConfig) -> tuple[bool, str]:
    """Возвращает (является_ли_ЛПР, причина).

    Сначала проверяются стоп-слова: если в любом сканируемом поле встречается
    стоп-слово, контакт исключается (даже при совпадении ключевого слова).
    Затем ищутся ключевые слова в порядке приоритета (сверху списка — выше).
    Причина — человекочитаемое объяснение для отчёта, например
    «Должность: «Генеральный директор» (ключевое слово «директор»)».
    """
    rank, reason = lpr_keyword_rank(contact, config)
    if rank is None:
        return False, reason
    return True, reason
