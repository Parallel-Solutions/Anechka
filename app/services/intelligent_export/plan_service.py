"""Parse + validate an ExportPlan (any schema version) into a 2.0 plan.

Single entry point used by chat, preview and run so the rest of the pipeline
only deals with validated 2.0 plans. Performs:
1. schema detection + v1 -> v2 adaptation;
2. Pydantic parse (shape/limits);
3. structural + catalog + scope validation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.services.export_plan.adapter import adapt_v1_to_v2
from app.services.export_plan.catalog import FieldCatalog
from app.services.export_plan.catalog_validator import CatalogScopeValidator
from app.services.export_plan.models import ExportPlan as ExportPlanV1
from app.services.export_plan.models_v2 import ExportPlan2
from app.services.export_plan.plan_normalizer import normalize_llm_plan
from app.services.export_plan.validator import ExportScope, ValidationResult
from app.services.intelligent_export.plan_enricher import sanitize_tomoru_plan


@dataclass
class PreparedPlan:
    plan: ExportPlan2 | None
    validation: ValidationResult
    catalog: FieldCatalog
    catalog_hash: str

    @property
    def valid(self) -> bool:
        return self.plan is not None and self.validation.valid


def issues_to_list(result: ValidationResult) -> list[dict]:
    return [
        {"code": i.code, "message": i.message, "path": i.path, "severity": i.severity}
        for i in result.issues
    ]


_FIELD_CODE_RE = re.compile(r"Field ([A-Za-z0-9_]+)")
_ENTITY_RE = re.compile(r"entity_type_id=(\d+)")
_FILTER_FIELD_RE = re.compile(r"for ([A-Za-z0-9_]+) \(")


def _suggest_fields(catalog: FieldCatalog, query: str, entity_type_id: int | None) -> list[dict]:
    matches = catalog.search(query, entity_type_id=entity_type_id, limit=5)
    return [
        {
            "entity_type_id": e.entity_type_id,
            "field_code": e.field_code,
            "display_name": e.display_name,
            "data_type": e.data_type,
            "allowed_filter_ops": list(e.allowed_filter_ops),
        }
        for e in matches
    ]


def enrich_issues(result: ValidationResult, catalog: FieldCatalog) -> list[dict]:
    """Validation errors enriched with actionable hints for planner self-repair.

    Adds, per error code: a human ``hint`` and, where useful, ``suggestions`` of
    real catalog fields / allowed operators so the planner can converge instead
    of guessing again.
    """
    enriched: list[dict] = []
    for issue in result.issues:
        item: dict = {
            "code": issue.code,
            "message": issue.message,
            "path": issue.path,
            "severity": issue.severity,
        }
        code = issue.code
        msg = issue.message or ""
        if code == "FIELD_NOT_ALLOWED":
            field_match = _FIELD_CODE_RE.search(msg)
            entity_match = _ENTITY_RE.search(msg)
            field_code = field_match.group(1) if field_match else ""
            entity_id = int(entity_match.group(1)) if entity_match else None
            if "sensitive" in msg:
                item["hint"] = (
                    "Это чувствительное поле и недоступно для текущей роли — убери его из плана."
                )
            else:
                item["hint"] = (
                    "Поля нет в каталоге для этой сущности. Возьми подходящий field_code из "
                    "suggestions или из context.catalog."
                )
                if field_code:
                    item["suggestions"] = _suggest_fields(catalog, field_code, entity_id)
        elif code == "FILTER_OP_NOT_ALLOWED":
            field_match = _FILTER_FIELD_RE.search(msg)
            field_code = field_match.group(1) if field_match else ""
            entry = None
            if field_code:
                for e in catalog.fields.values():
                    if e.field_code == field_code.upper():
                        entry = e
                        break
            if entry is not None:
                item["hint"] = f"Используй один из допустимых операторов: {list(entry.allowed_filter_ops)}."
                item["allowed_filter_ops"] = list(entry.allowed_filter_ops)
            else:
                item["hint"] = "Оператор не подходит для типа поля — выбери допустимый из allowed_filter_ops."
        elif code == "FIELD_NOT_SORTABLE":
            item["hint"] = "Это поле нельзя использовать для сортировки — убери его из sort."
        elif code == "FIELD_NOT_GROUPABLE":
            item["hint"] = "Это поле нельзя использовать для группировки — выбери groupable-поле (enum/status/user/boolean)."
        elif code == "SCOPE_ASSIGNED_REQUIRED":
            item["hint"] = "Добавь фильтр ASSIGNED_BY_ID eq <crm user id> в каждый dataset (требование роли viewer)."
        elif code == "ROW_LIMIT_EXCEEDED":
            item["hint"] = "Уменьши limit датасета до значения в пределах scope.max_rows."
        elif code == "ENTITY_TYPE_DENIED":
            item["hint"] = "Эта сущность недоступна для роли — используй только разрешённые entity_type_id из scope."
        elif code == "SCHEMA_INVALID":
            path = issue.path or ""
            msg_lower = msg.lower()
            if ".sort." in path and (path.endswith(".op") or "extra" in msg_lower):
                item["hint"] = (
                    'В элементе sort используется direction:"asc"|"desc", не op. '
                    "Замени ключ op на direction."
                )
            else:
                item["hint"] = (
                    "Структура плана не соответствует схеме ExportPlan 2.0 — "
                    "проверь обязательные поля и типы."
                )
        enriched.append(item)
    return enriched


_MAX_FIX_SUGGESTIONS = 10


def _format_one_fix_suggestion(item: dict) -> str:
    code = item.get("code", "")
    msg = item.get("message") or ""

    if code == "FIELD_NOT_ALLOWED":
        field_match = _FIELD_CODE_RE.search(msg)
        field_code = field_match.group(1) if field_match else ""
        if "sensitive" in msg and field_code:
            return f"Уберите поле {field_code} — оно недоступно для вашей роли."
        if field_code:
            alts = item.get("suggestions") or []
            if alts:
                parts = [f"{s['field_code']} ({s['display_name']})" for s in alts[:5]]
                return f"Поле {field_code} не найдено. Замените на: {', '.join(parts)}."
            return f"Поле {field_code} не найдено в каталоге — выберите поле из каталога CRM."

    if code == "FILTER_OP_NOT_ALLOWED":
        field_match = _FILTER_FIELD_RE.search(msg)
        field_code = field_match.group(1) if field_match else ""
        ops = item.get("allowed_filter_ops")
        if field_code and ops:
            return f"Для {field_code} оператор недопустим. Используйте: {', '.join(ops)}."

    hint = item.get("hint")
    if hint:
        path = item.get("path")
        return f"{hint} ({path})" if path else hint

    if msg:
        path = item.get("path")
        return f"{msg} ({path})" if path else msg
    return ""


def format_fix_suggestions(catalog: FieldCatalog, validation: ValidationResult) -> list[str]:
    """Human-readable fix options for chat when auto-repair is exhausted."""
    suggestions: list[str] = []
    seen: set[str] = set()
    for item in enrich_issues(validation, catalog):
        line = _format_one_fix_suggestion(item)
        if line and line not in seen:
            seen.add(line)
            suggestions.append(line)
            if len(suggestions) >= _MAX_FIX_SUGGESTIONS:
                break
    return suggestions


def format_repair_failure_message(max_repair_attempts: int) -> str:
    n = max(0, max_repair_attempts)
    if n % 10 == 1 and n % 100 != 11:
        word = "попытка"
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        word = "попытки"
    else:
        word = "попыток"
    return f"Не удалось автоматически исправить план ({n} {word}). Ниже — что можно сделать:"


def validation_to_dict(
    result: ValidationResult,
    *,
    status: str | None = None,
    memory_used: list | None = None,
    fix_suggestions: list | None = None,
) -> dict:
    return {
        "valid": result.valid,
        "status": status or ("valid" if result.valid else "invalid"),
        "issues": issues_to_list(result),
        "memory_used": memory_used or [],
        "fix_suggestions": fix_suggestions or [],
    }


def parse_plan(plan_dict: dict) -> tuple[ExportPlan2 | None, ValidationResult]:
    result = ValidationResult(valid=True)
    plan_dict = normalize_llm_plan(plan_dict)
    version = str(plan_dict.get("schema_version", "2.0"))
    try:
        if version == "1.0":
            v1 = ExportPlanV1.model_validate(plan_dict)
            plan = adapt_v1_to_v2(v1)
        else:
            plan = ExportPlan2.model_validate(plan_dict)
        return plan, result
    except ValidationError as exc:
        for err in exc.errors()[:20]:
            loc = ".".join(str(p) for p in err.get("loc", []))
            result.add("SCHEMA_INVALID", err.get("msg", "invalid"), loc)
        return None, result


def prepare_plan(
    db: Session,
    portal_id: str,
    scope: ExportScope,
    plan_dict: dict,
    *,
    denied_field_codes=None,
) -> PreparedPlan:
    catalog = FieldCatalog.load(db, portal_id, denied_field_codes=denied_field_codes)
    catalog_hash = catalog.snapshot_hash()
    plan_dict = normalize_llm_plan(plan_dict)
    plan_dict = sanitize_tomoru_plan(plan_dict)
    plan, result = parse_plan(plan_dict)
    if plan is None:
        return PreparedPlan(plan=None, validation=result, catalog=catalog, catalog_hash=catalog_hash)
    validation = CatalogScopeValidator(catalog, scope).validate(plan)
    return PreparedPlan(plan=plan, validation=validation, catalog=catalog, catalog_hash=catalog_hash)
