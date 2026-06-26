"""Human-readable summaries and assistant messages for intelligent export chat."""

from __future__ import annotations

from typing import Any

from app.models import ENTITY_COMPANY, ENTITY_CONTACT, ENTITY_DEAL, ENTITY_LEAD
from app.services.export_plan.catalog import FieldCatalog
from app.services.export_plan.models_v2 import ExportPlan2, FieldRef
from app.services.export_plan.registry import get_relation

_ENTITY_LABELS = {
    ENTITY_LEAD: "Лиды",
    ENTITY_DEAL: "Сделки",
    ENTITY_CONTACT: "Контакты",
    ENTITY_COMPANY: "Компании",
}

_OP_LABELS = {
    "eq": "равно",
    "ne": "не равно",
    "contains": "содержит",
    "starts_with": "начинается с",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "in": "в списке",
    "not_in": "не в списке",
    "is_null": "пусто",
    "is_not_null": "не пусто",
}


def _entity_label(entity_type_id: int) -> str:
    return _ENTITY_LABELS.get(entity_type_id, f"Сущность {entity_type_id}")


def _field_label(catalog: FieldCatalog, ref: FieldRef) -> str:
    entry = catalog.get(ref.entity_type_id, ref.field_code)
    display = entry.display_name if entry else ref.field_code
    alias = ref.source_alias or "?"
    return f"{_entity_label(ref.entity_type_id)}.{display} ({alias})"


def _describe_value(value: Any, catalog: FieldCatalog) -> str:
    kind = getattr(value, "kind", None)
    if kind == "field":
        return _field_label(catalog, value.field)
    if kind == "concat":
        codes = []
        for part in value.parts:
            if getattr(part, "kind", None) == "field":
                codes.append(part.field.field_code)
        if set(codes) >= {"LAST_NAME", "NAME"} or "SECOND_NAME" in codes:
            return "ФИО (фамилия + имя + отчество)"
        return "конкатенация полей"
    if kind == "coalesce":
        parts_desc = [_describe_value(p, catalog) for p in value.parts]
        return " или ".join(parts_desc)
    if kind == "constant":
        return f"«{value.value}»"
    return str(kind or "значение")


def build_plan_summary(plan: ExportPlan2, catalog: FieldCatalog) -> dict[str, Any]:
    entity_ids: set[int] = set()
    relations: list[str] = []
    filters: list[dict[str, Any]] = []
    limit: int | None = None

    for dataset in plan.datasets:
        limit = dataset.limit
        for source in dataset.sources:
            entity_ids.add(source.entity_type_id)
        for ref in dataset.relation_refs:
            rel = get_relation(ref.relation_code)
            relations.append(rel.description if rel else ref.relation_code)
        for cond in dataset.filters:
            filters.append(
                {
                    "field": _field_label(catalog, cond.field),
                    "op": _OP_LABELS.get(cond.op, cond.op),
                    "value": cond.value,
                    "values": cond.values,
                }
            )

    columns: list[dict[str, Any]] = []
    for sheet in plan.workbook.sheets:
        if sheet.mode != "rows":
            continue
        for col in sheet.columns:
            transforms = [t.op for t in col.transforms]
            columns.append(
                {
                    "header": col.header,
                    "source": _describe_value(col.value, catalog),
                    "transforms": transforms,
                }
            )

    assumptions: list[str] = []
    if relations:
        assumptions.append(
            "Несколько контактов у одной сделки/лида дают несколько строк — это ожидаемо."
        )
    assumptions.append("Данные берутся из последнего импорта CRM в локальную базу.")

    return {
        "title": plan.title,
        "entities": [_entity_label(eid) for eid in sorted(entity_ids)],
        "relations": relations,
        "columns": columns,
        "filters": filters,
        "limit": limit,
        "assumptions": assumptions,
    }


def _summary_text(summary: dict[str, Any]) -> str:
    lines: list[str] = []
    if summary.get("title"):
        lines.append(f"План: {summary['title']}.")
    if summary.get("entities"):
        lines.append(f"Сущности: {', '.join(summary['entities'])}.")
    if summary.get("relations"):
        lines.append(f"Связи: {', '.join(summary['relations'])}.")
    if summary.get("columns"):
        col_parts = []
        for col in summary["columns"]:
            part = col["header"]
            if col.get("source"):
                part += f" ({col['source']})"
            if col.get("transforms"):
                part += f" [{', '.join(col['transforms'])}]"
            col_parts.append(part)
        lines.append("Колонки: " + "; ".join(col_parts) + ".")
    if summary.get("filters"):
        filter_parts = []
        for f in summary["filters"]:
            val = f.get("value")
            if val is not None:
                filter_parts.append(f"{f['field']} {f['op']} «{val}»")
            elif f.get("values"):
                filter_parts.append(f"{f['field']} {f['op']}")
            else:
                filter_parts.append(f"{f['field']} {f['op']}")
        lines.append("Фильтры: " + "; ".join(filter_parts) + ".")
    if summary.get("limit"):
        lines.append(f"Лимит строк: {summary['limit']}.")
    for assumption in summary.get("assumptions") or []:
        lines.append(assumption)
    return "\n".join(lines)


def _is_echo_message(llm_message: str, summary: dict[str, Any]) -> bool:
    text = (llm_message or "").strip()
    lower = text.lower()
    if any(marker in lower for marker in ("колонк", "фио", "фильтр", "сущност", "связ")):
        return False
    if len(text) < 80:
        return True
    title = (summary.get("title") or "").lower()
    if title and title in lower:
        return True
    return False


def format_assistant_message(
    llm_message: str,
    summary: dict[str, Any] | None,
    *,
    status: str,
    fix_suggestions: list[str] | None = None,
) -> str:
    if status == "rejected":
        parts = [llm_message.strip()] if llm_message and llm_message.strip() else []
        if fix_suggestions:
            parts.append("Рекомендации по исправлению:\n" + "\n".join(f"• {s}" for s in fix_suggestions))
        return "\n\n".join(parts) if parts else llm_message

    if summary is None:
        return llm_message

    server_text = _summary_text(summary)
    llm = (llm_message or "").strip()
    if not llm or _is_echo_message(llm, summary):
        return server_text
    if server_text.lower() in llm.lower():
        return llm
    return f"{llm}\n\n{server_text}"
