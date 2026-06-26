"""Pydantic models for ExportPlan v1.0 — mirrors export_plan.schema.json."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

EntityTypeId = Literal[1, 2, 3, 4]
FilterOp = Literal[
    "eq", "ne", "gt", "gte", "lt", "lte",
    "in", "not_in", "contains", "starts_with",
    "is_null", "is_not_null",
]
TransformOp = Literal[
    "none", "trim", "uppercase", "lowercase",
    "phone_normalize", "phone_format_display",
    "date_format", "number_format", "dictionary_label",
    "mapping_lookup", "default_value",
]


class FieldRef(BaseModel):
    entity_type_id: EntityTypeId
    field_code: str = Field(min_length=1, max_length=255)
    source_alias: str | None = None


class Filter(BaseModel):
    field: FieldRef
    op: FilterOp
    value: Any | None = None
    values: list[Any] | None = None

    @model_validator(mode="after")
    def check_value_presence(self) -> Filter:
        if self.op in ("is_null", "is_not_null"):
            return self
        if self.op in ("in", "not_in"):
            if not self.values:
                raise ValueError(f"Filter op {self.op} requires values")
            return self
        if self.value is None:
            raise ValueError(f"Filter op {self.op} requires value")
        return self


class Source(BaseModel):
    alias: str = Field(min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_]*$")
    entity_type_id: EntityTypeId
    filters: list[Filter] = Field(default_factory=list, max_length=30)
    include_deleted: bool = False
    limit: int | None = Field(default=None, ge=1, le=50000)


class Join(BaseModel):
    type: Literal["inner", "left"]
    from_alias: str
    to_alias: str
    from_field: FieldRef
    to_field: FieldRef


class Column(BaseModel):
    header: str = Field(min_length=1, max_length=120)
    field: FieldRef
    transform_id: str | None = None
    sheet: str | None = Field(default=None, max_length=31)
    width: int | None = Field(default=None, ge=5, le=100)


class Transform(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    op: TransformOp
    params: dict[str, Any] = Field(default_factory=dict)


class SortKey(BaseModel):
    field: FieldRef
    direction: Literal["asc", "desc"]


class ValidationRule(BaseModel):
    id: str
    type: Literal["required", "regex", "min_length", "max_length", "in_dictionary"]
    field: FieldRef
    params: dict[str, Any] = Field(default_factory=dict)
    severity: Literal["error", "warning"] = "error"


class OutputSheet(BaseModel):
    name: str = Field(min_length=1, max_length=31)
    column_headers: list[str] | None = None
    include_errors: bool = False


class Output(BaseModel):
    format: Literal["xlsx", "csv"]
    sheets: list[OutputSheet] = Field(default_factory=lambda: [OutputSheet(name="Данные")])
    include_params_sheet: bool = True
    filename_label: str | None = Field(default=None, max_length=60)


class MemoryRef(BaseModel):
    memory_id: int
    kind: Literal["term", "alias", "mapping", "template", "rule"]


class ExportPlan(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    sources: list[Source] = Field(min_length=1, max_length=10)
    joins: list[Join] = Field(default_factory=list, max_length=10)
    columns: list[Column] = Field(default_factory=list, max_length=200)
    transforms: list[Transform] = Field(default_factory=list, max_length=50)
    sort: list[SortKey] = Field(default_factory=list, max_length=5)
    group_by: list[FieldRef] = Field(default_factory=list, max_length=10)
    validation_rules: list[ValidationRule] = Field(default_factory=list, max_length=20)
    output: Output
    memory_refs: list[MemoryRef] = Field(default_factory=list, max_length=20)

    @field_validator("sources")
    @classmethod
    def unique_aliases(cls, sources: list[Source]) -> list[Source]:
        aliases = [s.alias for s in sources]
        if len(aliases) != len(set(aliases)):
            raise ValueError("Source aliases must be unique")
        return sources

    @model_validator(mode="after")
    def default_source_alias_on_fields(self) -> ExportPlan:
        alias_by_type = {s.entity_type_id: s.alias for s in self.sources}
        for col in self.columns:
            if col.field.source_alias is None:
                col.field.source_alias = alias_by_type.get(col.field.entity_type_id)
        return self
