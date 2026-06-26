"""Server-side registries for ExportPlan 2.0.

These registries are the single source of truth for:
- approved entity relations (JOINs) — AI references a ``relation_code`` only;
- transformation operations and their typed parameter schemas;
- row validation rules and their typed parameter schemas.

AI never supplies SQL, JSONPath, table/column names or arbitrary expressions.
It can only reference identifiers that exist here, and every reference is
re-checked on the server.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.models import ENTITY_COMPANY, ENTITY_CONTACT, ENTITY_DEAL, ENTITY_LEAD

# ---------------------------------------------------------------------------
# Relations (approved JOINs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelationDef:
    relation_code: str
    from_entity_type_id: int
    to_entity_type_id: int
    join_type: str  # "left" | "inner"
    from_field_code: str  # field on the "from" entity holding the related id
    to_field_code: str  # field on the "to" entity (normally ID)
    description: str
    # Junction-link relations (Variant A): join through crm_contact_links instead
    # of a direct payload FK. When via_table is set, from_field_code/to_field_code
    # are ignored by the compiler.
    via_table: str | None = None  # e.g. "crm_contact_links"
    via_parent_type_id: int | None = None  # 2 for deal, 1 for lead (parent_entity_type_id)
    primary_only: bool = False  # only is_primary=True links


RELATIONS: dict[str, RelationDef] = {
    r.relation_code: r
    for r in (
        RelationDef("deal_contact", ENTITY_DEAL, ENTITY_CONTACT, "left", "CONTACT_ID", "ID", "Контакт сделки"),
        RelationDef("deal_company", ENTITY_DEAL, ENTITY_COMPANY, "left", "COMPANY_ID", "ID", "Компания сделки"),
        RelationDef("lead_contact", ENTITY_LEAD, ENTITY_CONTACT, "left", "CONTACT_ID", "ID", "Контакт лида"),
        RelationDef("lead_company", ENTITY_LEAD, ENTITY_COMPANY, "left", "COMPANY_ID", "ID", "Компания лида"),
        RelationDef("contact_company", ENTITY_CONTACT, ENTITY_COMPANY, "left", "COMPANY_ID", "ID", "Компания контакта"),
        # Junction relations (Variant A) — join through crm_contact_links so ALL
        # linked contacts are pulled (the legacy *_contact joins use payload
        # contactId which is usually empty). Row explosion (one row per linked
        # contact) is expected.
        RelationDef(
            "deal_contact_link", ENTITY_DEAL, ENTITY_CONTACT, "left", "ID", "ID",
            "Контакты сделки (все, через привязки CRM)",
            via_table="crm_contact_links", via_parent_type_id=ENTITY_DEAL,
        ),
        RelationDef(
            "deal_primary_contact_link", ENTITY_DEAL, ENTITY_CONTACT, "left", "ID", "ID",
            "Основной контакт сделки (через привязки CRM)",
            via_table="crm_contact_links", via_parent_type_id=ENTITY_DEAL, primary_only=True,
        ),
        RelationDef(
            "lead_contact_link", ENTITY_LEAD, ENTITY_CONTACT, "left", "ID", "ID",
            "Контакты лида (все, через привязки CRM)",
            via_table="crm_contact_links", via_parent_type_id=ENTITY_LEAD,
        ),
        RelationDef(
            "lead_primary_contact_link", ENTITY_LEAD, ENTITY_CONTACT, "left", "ID", "ID",
            "Основной контакт лида (через привязки CRM)",
            via_table="crm_contact_links", via_parent_type_id=ENTITY_LEAD, primary_only=True,
        ),
    )
}


def get_relation(relation_code: str) -> RelationDef | None:
    return RELATIONS.get(relation_code)


# ---------------------------------------------------------------------------
# Transform operations
# ---------------------------------------------------------------------------


class TrimParams(BaseModel):
    model_config = {"extra": "forbid"}


class CaseParams(BaseModel):
    model_config = {"extra": "forbid"}


class PhoneNormalizeParams(BaseModel):
    model_config = {"extra": "forbid"}
    country: str = Field(default="RU", min_length=2, max_length=2)


class PhoneDigitsOnlyParams(BaseModel):
    model_config = {"extra": "forbid"}


class DateFormatParams(BaseModel):
    model_config = {"extra": "forbid"}
    format: str = Field(default="%d.%m.%Y", min_length=1, max_length=64)
    timezone: str = Field(default="Europe/Moscow", max_length=64)
    source_format: str | None = Field(default=None, max_length=64)


class NumberRoundParams(BaseModel):
    model_config = {"extra": "forbid"}
    digits: int = Field(default=2, ge=0, le=10)


class DictionaryLabelParams(BaseModel):
    model_config = {"extra": "forbid"}
    dictionary_code: str | None = Field(default=None, max_length=255)


class MappingLookupParams(BaseModel):
    model_config = {"extra": "forbid"}
    mapping: dict[str, str] = Field(default_factory=dict)
    on_unknown: str = Field(default="keep_original")  # error|warning|keep_original|default
    default: str | None = None

    def model_post_init(self, _ctx: Any) -> None:
        if self.on_unknown not in ("error", "warning", "keep_original", "default"):
            raise ValueError("on_unknown must be one of error|warning|keep_original|default")
        if len(self.mapping) > 1000:
            raise ValueError("mapping too large")


class DefaultValueParams(BaseModel):
    model_config = {"extra": "forbid"}
    value: str | int | float | bool | None = ""


class NullToEmptyParams(BaseModel):
    model_config = {"extra": "forbid"}


class ConstantParams(BaseModel):
    model_config = {"extra": "forbid"}
    value: str | int | float | bool | None = ""


@dataclass(frozen=True)
class TransformSpec:
    op: str
    param_model: type[BaseModel]
    description: str
    applies_to: tuple[str, ...]  # input data types, "*" = any


TRANSFORMS: dict[str, TransformSpec] = {
    "trim": TransformSpec("trim", TrimParams, "Убрать пробелы по краям", ("string",)),
    "uppercase": TransformSpec("uppercase", CaseParams, "В ВЕРХНИЙ регистр", ("string",)),
    "lowercase": TransformSpec("lowercase", CaseParams, "в нижний регистр", ("string",)),
    "phone_normalize": TransformSpec("phone_normalize", PhoneNormalizeParams, "Нормализовать телефон (+7...)", ("string",)),
    "phone_digits_only": TransformSpec("phone_digits_only", PhoneDigitsOnlyParams, "Оставить только цифры телефона", ("string",)),
    "date_format": TransformSpec("date_format", DateFormatParams, "Форматировать дату", ("date", "datetime", "string")),
    "number_round": TransformSpec("number_round", NumberRoundParams, "Округлить число", ("number",)),
    "dictionary_label": TransformSpec("dictionary_label", DictionaryLabelParams, "Подставить значение справочника", ("*",)),
    "mapping_lookup": TransformSpec("mapping_lookup", MappingLookupParams, "Сопоставление значений", ("*",)),
    "default_value": TransformSpec("default_value", DefaultValueParams, "Значение по умолчанию, если пусто", ("*",)),
    "null_to_empty": TransformSpec("null_to_empty", NullToEmptyParams, "NULL → пустая строка", ("*",)),
    "constant": TransformSpec("constant", ConstantParams, "Заменить на константу", ("*",)),
}


def validate_transform_params(op: str, params: dict[str, Any]) -> tuple[BaseModel | None, str | None]:
    """Returns (parsed_params, error). error is None on success."""
    spec = TRANSFORMS.get(op)
    if spec is None:
        return None, f"Unknown transform op: {op}"
    try:
        return spec.param_model(**(params or {})), None
    except ValidationError as exc:
        return None, f"Invalid params for transform {op}: {exc.errors()[:3]}"
    except ValueError as exc:
        return None, f"Invalid params for transform {op}: {exc}"


# ---------------------------------------------------------------------------
# Validation rules
# ---------------------------------------------------------------------------

MAX_REGEX_LENGTH = 200


class NoParams(BaseModel):
    model_config = {"extra": "forbid"}


class DateRuleParams(BaseModel):
    model_config = {"extra": "forbid"}
    format: str | None = Field(default=None, max_length=64)


class LengthParams(BaseModel):
    model_config = {"extra": "forbid"}
    value: int = Field(ge=0, le=100000)


class RegexParams(BaseModel):
    model_config = {"extra": "forbid"}
    pattern: str = Field(min_length=1, max_length=MAX_REGEX_LENGTH)

    def model_post_init(self, _ctx: Any) -> None:
        # Reject catastrophic backtracking primitives in free input.
        suspicious = ("(.*)+", "(.+)+", "(.*)*", "(.+)*")
        if any(tok in self.pattern for tok in suspicious):
            raise ValueError("regex pattern too complex")


class InDictionaryParams(BaseModel):
    model_config = {"extra": "forbid"}
    dictionary_code: str = Field(min_length=1, max_length=255)


@dataclass(frozen=True)
class ValidationRuleSpec:
    rule_type: str
    param_model: type[BaseModel]
    description: str


VALIDATION_RULES: dict[str, ValidationRuleSpec] = {
    "required": ValidationRuleSpec("required", NoParams, "Значение обязательно"),
    "string": ValidationRuleSpec("string", NoParams, "Должно быть строкой"),
    "number": ValidationRuleSpec("number", NoParams, "Должно быть числом"),
    "date": ValidationRuleSpec("date", DateRuleParams, "Должно быть датой"),
    "min_length": ValidationRuleSpec("min_length", LengthParams, "Минимальная длина"),
    "max_length": ValidationRuleSpec("max_length", LengthParams, "Максимальная длина"),
    "regex": ValidationRuleSpec("regex", RegexParams, "Соответствие шаблону"),
    "in_dictionary": ValidationRuleSpec("in_dictionary", InDictionaryParams, "Значение из справочника"),
    "unique": ValidationRuleSpec("unique", NoParams, "Уникальное значение"),
    "not_empty_after_transform": ValidationRuleSpec(
        "not_empty_after_transform", NoParams, "Не пусто после преобразований"
    ),
}


def validate_rule_params(rule_type: str, params: dict[str, Any]) -> tuple[BaseModel | None, str | None]:
    spec = VALIDATION_RULES.get(rule_type)
    if spec is None:
        return None, f"Unknown validation rule type: {rule_type}"
    try:
        return spec.param_model(**(params or {})), None
    except ValidationError as exc:
        return None, f"Invalid params for rule {rule_type}: {exc.errors()[:3]}"
    except ValueError as exc:
        return None, f"Invalid params for rule {rule_type}: {exc}"


def registry_descriptor() -> dict[str, Any]:
    """Compact descriptor for the Metadata Catalog / planner prompt."""
    return {
        "relations": [
            {
                "relation_code": r.relation_code,
                "from_entity_type_id": r.from_entity_type_id,
                "to_entity_type_id": r.to_entity_type_id,
                "join_type": r.join_type,
                "description": r.description,
            }
            for r in RELATIONS.values()
        ],
        "transforms": [{"op": s.op, "description": s.description, "applies_to": list(s.applies_to)} for s in TRANSFORMS.values()],
        "validation_rules": [{"type": s.rule_type, "description": s.description} for s in VALIDATION_RULES.values()],
    }
