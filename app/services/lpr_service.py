"""Эвристическое определение ЛПР (лиц, принимающих решения).

Список ключевых слов и сканируемых полей хранится в app_settings и
редактируется пользователем на странице настроек. Если значения не заданы,
используются разумные значения по умолчанию (исследованы на живом Bitrix:
основной сигнал — поле POST = «Должность»).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models import AppSetting, utcnow

logger = logging.getLogger(__name__)

LPR_KEYWORDS_KEY = "lpr_keywords"
LPR_FIELDS_KEY = "lpr_fields"
LPR_STOPWORDS_KEY = "lpr_stopwords"

DEFAULT_LPR_KEYWORDS: list[str] = [
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

# Поля контакта, в которых ищем признаки ЛПР. На исследованном портале
# осмысленный сигнал даёт только POST (Должность); пользователь может
# расширить список (например, COMMENTS или пользовательские UF-поля).
DEFAULT_LPR_FIELDS: list[str] = ["POST"]

# Стоп-слова: контакт исключается из ЛПР, если в сканируемых полях встречается
# любое из них (даже при совпадении ключевого слова). Например, «бывш начальник»
# содержит ключевое «начальник», но стоп-слово «бывш» исключает контакт.
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
    keywords = _normalize_list(_get_setting(db, LPR_KEYWORDS_KEY), DEFAULT_LPR_KEYWORDS)
    fields = _normalize_list(_get_setting(db, LPR_FIELDS_KEY), DEFAULT_LPR_FIELDS)
    stopwords = _normalize_list(_get_setting(db, LPR_STOPWORDS_KEY), DEFAULT_LPR_STOPWORDS)
    return LprConfig(keywords=keywords, fields=fields, stopwords=stopwords)


def save_lpr_config(
    db: Session,
    keywords: list[str],
    fields: list[str],
    stopwords: list[str],
) -> LprConfig:
    clean_keywords = _normalize_list(keywords, DEFAULT_LPR_KEYWORDS)
    clean_fields = _normalize_list(fields, DEFAULT_LPR_FIELDS)
    clean_stopwords = _normalize_list(stopwords, DEFAULT_LPR_STOPWORDS)
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


def _field_text(contact: dict[str, Any], field: str) -> str:
    raw = contact.get(field)
    if raw is None:
        return ""
    if isinstance(raw, (list, tuple)):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                parts.append(str(item.get("VALUE", "")))
            else:
                parts.append(str(item))
        return " ".join(p for p in parts if p)
    return str(raw)


def detect_lpr(contact: dict[str, Any], config: LprConfig) -> tuple[bool, str]:
    """Возвращает (является_ли_ЛПР, причина).

    Сначала проверяются стоп-слова: если в любом сканируемом поле встречается
    стоп-слово, контакт исключается (даже при совпадении ключевого слова).
    Затем ищутся ключевые слова. Причина — человекочитаемое объяснение для
    отчёта, например «Должность: «Генеральный директор» (ключевое слово «директор»)».
    """
    field_texts = [(fld, _field_text(contact, fld)) for fld in config.fields]

    lowered_stop = [sw.lower() for sw in config.stopwords if sw]
    for _fld, text in field_texts:
        if not text:
            continue
        haystack = text.lower()
        if any(sw in haystack for sw in lowered_stop):
            return False, ""

    lowered_keywords = [(kw, kw.lower()) for kw in config.keywords if kw]
    for fld, text in field_texts:
        if not text:
            continue
        haystack = text.lower()
        for original_kw, kw in lowered_keywords:
            if kw in haystack:
                title = FIELD_TITLES.get(fld, fld)
                return True, f"{title}: «{text.strip()}» (ключевое слово «{original_kw}»)"
    return False, ""
