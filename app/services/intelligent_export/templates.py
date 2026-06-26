"""Deterministic "Quick export" templates.

These are curated ExportPlan 2.0 plans built only from guaranteed catalog
fields (denormalized system columns + well-known multifields). They are
validated against the live catalog/scope like any other plan, but they never
involve the LLM — giving a 100%-predictable path for the most common exports
("все сделки", "все контакты", ...). This is the reliable fallback when the AI
planner is slow, unavailable, or the request is a simple, standard one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from app.models import ENTITY_COMPANY, ENTITY_CONTACT, ENTITY_DEAL, ENTITY_LEAD
from app.services.intelligent_export.date_tokens import resolve_date_tokens


def _field(entity: int, code: str) -> dict[str, Any]:
    return {"entity_type_id": entity, "field_code": code}


def _inject_alias(node: Any, alias: str) -> None:
    """Set ``source_alias`` on every field ref that omits it.

    Sheet-level refs are auto-filled by the model, but dataset-level sort/filter
    refs are not — so we set the alias explicitly everywhere to keep templates
    valid regardless of where the ref lives.
    """
    if isinstance(node, dict):
        if "entity_type_id" in node and "field_code" in node:
            node.setdefault("source_alias", alias)
        for value in node.values():
            _inject_alias(value, alias)
    elif isinstance(node, list):
        for item in node:
            _inject_alias(item, alias)


def _col(cid: str, header: str, entity: int, code: str, transforms: list[dict] | None = None) -> dict[str, Any]:
    col: dict[str, Any] = {
        "id": cid,
        "header": header,
        "value": {"kind": "field", "field": _field(entity, code)},
    }
    if transforms:
        col["transforms"] = transforms
    return col


_DICT = [{"op": "dictionary_label", "params": {}}]
_DATE = [{"op": "date_format", "params": {}}]
_MONEY = [{"op": "number_round", "params": {"digits": 2}}]
_PHONE = [{"op": "phone_normalize", "params": {}}]


def _deal_columns() -> list[dict]:
    return [
        _col("id", "ID", ENTITY_DEAL, "ID"),
        _col("title", "Название", ENTITY_DEAL, "TITLE"),
        _col("stage", "Стадия", ENTITY_DEAL, "STAGE_ID", _DICT),
        _col("amount", "Сумма", ENTITY_DEAL, "OPPORTUNITY", _MONEY),
        _col("currency", "Валюта", ENTITY_DEAL, "CURRENCY_ID"),
        _col("assigned", "Ответственный", ENTITY_DEAL, "ASSIGNED_BY_ID", _DICT),
        _col("created", "Создана", ENTITY_DEAL, "DATE_CREATE", _DATE),
    ]


def _single_source_plan(
    *,
    title: str,
    entity: int,
    dataset_id: str,
    alias: str,
    sheet_name: str,
    columns: list[dict],
    filename_label: str,
    limit: int,
    filters: list[dict] | None = None,
    sort_code: str | None = "DATE_CREATE",
) -> dict[str, Any]:
    dataset: dict[str, Any] = {
        "id": dataset_id,
        "primary_entity_type_id": entity,
        "sources": [{"alias": alias, "entity_type_id": entity}],
        "filters": filters or [],
        "limit": limit,
    }
    if sort_code:
        dataset["sort"] = [{"field": _field(entity, sort_code), "direction": "desc"}]
    plan = {
        "schema_version": "2.0",
        "title": title,
        "datasets": [dataset],
        "workbook": {
            "format": "xlsx",
            "filename_label": filename_label,
            "sheets": [
                {
                    "id": "main",
                    "name": sheet_name,
                    "mode": "rows",
                    "dataset_id": dataset_id,
                    "columns": columns,
                }
            ],
        },
    }
    _inject_alias(plan, alias)
    return plan


def _deal_contacts_plan(*, limit: int) -> dict[str, Any]:
    """Deals joined to ALL linked contacts via crm_contact_links (Variant A)."""
    deal = "deal"
    contact = "contact"

    def deal_col(cid, header, code, transforms=None):
        col = {
            "id": cid,
            "header": header,
            "value": {"kind": "field", "field": {"entity_type_id": ENTITY_DEAL, "field_code": code, "source_alias": deal}},
        }
        if transforms:
            col["transforms"] = transforms
        return col

    def contact_col(cid, header, code, transforms=None):
        col = {
            "id": cid,
            "header": header,
            "value": {"kind": "field", "field": {"entity_type_id": ENTITY_CONTACT, "field_code": code, "source_alias": contact}},
        }
        if transforms:
            col["transforms"] = transforms
        return col

    def contact_fio_col(cid, header):
        return {
            "id": cid,
            "header": header,
            "value": {
                "kind": "coalesce",
                "parts": [
                    {
                        "kind": "concat",
                        "parts": [
                            {"kind": "field", "field": {"entity_type_id": ENTITY_CONTACT, "field_code": code, "source_alias": contact}}
                            for code in ("LAST_NAME", "NAME", "SECOND_NAME")
                        ],
                        "separator": " ",
                    },
                    {"kind": "field", "field": {"entity_type_id": ENTITY_CONTACT, "field_code": "TITLE", "source_alias": contact}},
                ],
            },
        }

    dataset = {
        "id": "deals_contacts",
        "primary_entity_type_id": ENTITY_DEAL,
        "sources": [
            {"alias": deal, "entity_type_id": ENTITY_DEAL},
            {"alias": contact, "entity_type_id": ENTITY_CONTACT},
        ],
        "relation_refs": [
            {"relation_code": "deal_contact_link", "from_alias": deal, "to_alias": contact}
        ],
        "filters": [],
        "sort": [{"field": {"entity_type_id": ENTITY_DEAL, "field_code": "DATE_CREATE", "source_alias": deal}, "direction": "desc"}],
        "limit": limit,
    }
    return {
        "schema_version": "2.0",
        "title": "Сделки с контактами",
        "datasets": [dataset],
        "workbook": {
            "format": "xlsx",
            "filename_label": "deals_with_contacts",
            "sheets": [
                {
                    "id": "main",
                    "name": "Сделки и контакты",
                    "mode": "rows",
                    "dataset_id": "deals_contacts",
                    "columns": [
                        deal_col("deal_id", "ID сделки", "ID"),
                        deal_col("deal_title", "Название", "TITLE"),
                        deal_col("deal_stage", "Стадия", "STAGE_ID", _DICT),
                        deal_col("deal_amount", "Сумма", "OPPORTUNITY", _MONEY),
                        contact_col("contact_id", "ID контакта", "ID"),
                        contact_fio_col("contact_name", "Контакт"),
                        contact_col("contact_phone", "Телефон контакта", "PHONE", _PHONE),
                        contact_col("contact_post", "Должность", "POST"),
                        contact_col("contact_comments", "Описание", "COMMENTS"),
                    ],
                }
            ],
        },
    }


@dataclass
class QuickTemplate:
    key: str
    title: str
    description: str
    build: Callable[[int, date], dict[str, Any]]


_TEMPLATES: list[QuickTemplate] = [
    QuickTemplate(
        key="all_deals",
        title="Все сделки",
        description="Сделки: ID, название, стадия, сумма, ответственный, дата создания.",
        build=lambda limit, today: _single_source_plan(
            title="Все сделки",
            entity=ENTITY_DEAL,
            dataset_id="deals",
            alias="deal",
            sheet_name="Сделки",
            columns=_deal_columns(),
            filename_label="all_deals",
            limit=limit,
        ),
    ),
    QuickTemplate(
        key="deals_this_month",
        title="Сделки за текущий месяц",
        description="Сделки, созданные с начала текущего месяца.",
        build=lambda limit, today: resolve_date_tokens(
            _single_source_plan(
                title="Сделки за текущий месяц",
                entity=ENTITY_DEAL,
                dataset_id="deals",
                alias="deal",
                sheet_name="Сделки",
                columns=_deal_columns(),
                filename_label="deals_this_month",
                limit=limit,
                filters=[
                    {
                        "field": _field(ENTITY_DEAL, "DATE_CREATE"),
                        "op": "gte",
                        "value": "@month_start",
                    }
                ],
            ),
            today,
        ),
    ),
    QuickTemplate(
        key="all_contacts",
        title="Все контакты",
        description="Контакты: ID, имя/название, ответственный, дата создания.",
        build=lambda limit, today: _single_source_plan(
            title="Все контакты",
            entity=ENTITY_CONTACT,
            dataset_id="contacts",
            alias="contact",
            sheet_name="Контакты",
            columns=[
                _col("id", "ID", ENTITY_CONTACT, "ID"),
                _col("title", "Имя/Название", ENTITY_CONTACT, "TITLE"),
                _col("assigned", "Ответственный", ENTITY_CONTACT, "ASSIGNED_BY_ID", _DICT),
                _col("created", "Создан", ENTITY_CONTACT, "DATE_CREATE", _DATE),
            ],
            filename_label="all_contacts",
            limit=limit,
        ),
    ),
    QuickTemplate(
        key="contacts_with_phones",
        title="Контакты с телефонами",
        description="Контакты с нормализованным телефоном и e-mail (чувствительные поля).",
        build=lambda limit, today: _single_source_plan(
            title="Контакты с телефонами",
            entity=ENTITY_CONTACT,
            dataset_id="contacts",
            alias="contact",
            sheet_name="Контакты",
            columns=[
                _col("id", "ID", ENTITY_CONTACT, "ID"),
                _col("title", "Имя/Название", ENTITY_CONTACT, "TITLE"),
                _col("phone", "Телефон", ENTITY_CONTACT, "PHONE", _PHONE),
                _col("email", "E-mail", ENTITY_CONTACT, "EMAIL"),
                _col("created", "Создан", ENTITY_CONTACT, "DATE_CREATE", _DATE),
            ],
            filename_label="contacts_with_phones",
            limit=limit,
        ),
    ),
    QuickTemplate(
        key="all_companies",
        title="Все компании",
        description="Компании: ID, название, ответственный, дата создания.",
        build=lambda limit, today: _single_source_plan(
            title="Все компании",
            entity=ENTITY_COMPANY,
            dataset_id="companies",
            alias="company",
            sheet_name="Компании",
            columns=[
                _col("id", "ID", ENTITY_COMPANY, "ID"),
                _col("title", "Название", ENTITY_COMPANY, "TITLE"),
                _col("assigned", "Ответственный", ENTITY_COMPANY, "ASSIGNED_BY_ID", _DICT),
                _col("created", "Создана", ENTITY_COMPANY, "DATE_CREATE", _DATE),
            ],
            filename_label="all_companies",
            limit=limit,
        ),
    ),
    QuickTemplate(
        key="all_leads",
        title="Все лиды",
        description="Лиды: ID, название, ответственный, дата создания.",
        build=lambda limit, today: _single_source_plan(
            title="Все лиды",
            entity=ENTITY_LEAD,
            dataset_id="leads",
            alias="lead",
            sheet_name="Лиды",
            columns=[
                _col("id", "ID", ENTITY_LEAD, "ID"),
                _col("title", "Название", ENTITY_LEAD, "TITLE"),
                _col("assigned", "Ответственный", ENTITY_LEAD, "ASSIGNED_BY_ID", _DICT),
                _col("created", "Создан", ENTITY_LEAD, "DATE_CREATE", _DATE),
            ],
            filename_label="all_leads",
            limit=limit,
        ),
    ),
    QuickTemplate(
        key="deals_with_contacts",
        title="Сделки с контактами",
        description="Сделки с привязанными контактами (через crm_contact_links): ID/название/стадия/сумма сделки; у контакта — ФИО, телефон, должность, описание. Несколько строк на сделку при нескольких контактах.",
        build=lambda limit, today: _deal_contacts_plan(limit=limit),
    ),
]

_BY_KEY = {t.key: t for t in _TEMPLATES}


def list_templates() -> list[dict[str, str]]:
    return [{"key": t.key, "title": t.title, "description": t.description} for t in _TEMPLATES]


def get_template(key: str) -> QuickTemplate | None:
    return _BY_KEY.get(key)


def build_template_plan(key: str, *, max_rows: int, today: date | None = None) -> dict[str, Any] | None:
    template = _BY_KEY.get(key)
    if template is None:
        return None
    limit = max(1, min(int(max_rows), 200000))
    return template.build(limit, today or date.today())
