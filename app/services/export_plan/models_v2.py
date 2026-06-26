"""Pydantic models for ExportPlan 2.0.

Independent per-sheet datasets, per-column value expressions (no free
expressions — only whitelisted kinds), per-transform/per-rule typed params,
and relation references resolved by the server registry.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, model_validator

EntityTypeId = Literal[1, 2, 3, 4]
FilterOp = Literal[
    "eq", "ne", "gt", "gte", "lt", "lte",
    "in", "not_in", "contains", "starts_with",
    "is_null", "is_not_null",
]
ALIAS_PATTERN = r"^[a-z][a-z0-9_]*$"
ID_PATTERN = r"^[a-z][a-z0-9_]*$"


class FieldRef(BaseModel):
    model_config = {"extra": "forbid"}
    entity_type_id: EntityTypeId
    field_code: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_]+$")
    source_alias: str | None = Field(default=None, max_length=32)


class Condition(BaseModel):
    model_config = {"extra": "forbid"}
    field: FieldRef
    op: FilterOp
    value: str | int | float | bool | None = None
    values: list[str | int | float | bool] | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _check_value_presence(self) -> "Condition":
        if self.op in ("is_null", "is_not_null"):
            return self
        if self.op in ("in", "not_in"):
            if not self.values:
                raise ValueError(f"op {self.op} requires non-empty 'values'")
            return self
        if self.value is None:
            raise ValueError(f"op {self.op} requires 'value'")
        return self


Filter = Condition


# --- column value expressions (discriminated by "kind") ---------------------


class FieldValue(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Literal["field"] = "field"
    field: FieldRef


class ConstantValue(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Literal["constant"] = "constant"
    value: str | int | float | bool | None = None


ValueAtom = Annotated[Union[FieldValue, ConstantValue], Field(discriminator="kind")]


class ConcatValue(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Literal["concat"] = "concat"
    parts: list[ValueAtom] = Field(min_length=1, max_length=20)
    separator: str = Field(default=" ", max_length=16)


CoalescePart = Annotated[
    Union[FieldValue, ConstantValue, ConcatValue],
    Field(discriminator="kind"),
]


class CoalesceValue(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Literal["coalesce"] = "coalesce"
    parts: list[CoalescePart] = Field(min_length=1, max_length=20)


class ConditionalCase(BaseModel):
    model_config = {"extra": "forbid"}
    when: Condition
    then: ValueAtom


class ConditionalValue(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Literal["conditional"] = "conditional"
    cases: list[ConditionalCase] = Field(min_length=1, max_length=20)
    default: ValueAtom | None = None


class AggregateValue(BaseModel):
    model_config = {"extra": "forbid"}
    kind: Literal["aggregate"] = "aggregate"
    func: Literal["count", "sum", "avg", "min", "max"]
    field: FieldRef | None = None


ColumnValue = Annotated[
    Union[FieldValue, ConstantValue, ConcatValue, CoalesceValue, ConditionalValue, AggregateValue],
    Field(discriminator="kind"),
]


class TransformStep(BaseModel):
    model_config = {"extra": "forbid"}
    op: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)


class Column(BaseModel):
    model_config = {"extra": "forbid"}
    id: str = Field(min_length=1, max_length=64, pattern=ID_PATTERN)
    header: str = Field(min_length=1, max_length=120)
    value: ColumnValue
    transforms: list[TransformStep] = Field(default_factory=list, max_length=10)
    width: int | None = Field(default=None, ge=3, le=120)
    excel_format: str | None = Field(default=None, max_length=32)


class SortKey(BaseModel):
    model_config = {"extra": "forbid"}
    field: FieldRef
    direction: Literal["asc", "desc"] = "asc"


class Source(BaseModel):
    model_config = {"extra": "forbid"}
    alias: str = Field(min_length=1, max_length=32, pattern=ALIAS_PATTERN)
    entity_type_id: EntityTypeId


class RelationRef(BaseModel):
    model_config = {"extra": "forbid"}
    relation_code: str = Field(min_length=1, max_length=64)
    from_alias: str = Field(min_length=1, max_length=32)
    to_alias: str = Field(min_length=1, max_length=32)


class Dataset(BaseModel):
    model_config = {"extra": "forbid"}
    id: str = Field(min_length=1, max_length=64, pattern=ID_PATTERN)
    primary_entity_type_id: EntityTypeId
    sources: list[Source] = Field(min_length=1, max_length=6)
    relation_refs: list[RelationRef] = Field(default_factory=list, max_length=6)
    filters: list[Filter] = Field(default_factory=list, max_length=40)
    sort: list[SortKey] = Field(default_factory=list, max_length=6)
    limit: int = Field(default=5000, ge=1, le=200000)
    include_deleted: bool = False

    @model_validator(mode="after")
    def _unique_aliases(self) -> "Dataset":
        aliases = [s.alias for s in self.sources]
        if len(aliases) != len(set(aliases)):
            raise ValueError("source aliases must be unique within a dataset")
        if self.primary_entity_type_id not in {s.entity_type_id for s in self.sources}:
            raise ValueError("primary_entity_type_id must match one of the sources")
        return self


class Aggregate(BaseModel):
    model_config = {"extra": "forbid"}
    id: str = Field(min_length=1, max_length=64, pattern=ID_PATTERN)
    func: Literal["count", "sum", "avg", "min", "max"]
    field: FieldRef | None = None


class ValidationRule(BaseModel):
    model_config = {"extra": "forbid"}
    id: str = Field(min_length=1, max_length=64)
    type: str = Field(min_length=1, max_length=64)
    column_id: str | None = Field(default=None, max_length=64)
    field: FieldRef | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    severity: Literal["error", "warning"] = "error"

    @model_validator(mode="after")
    def _target_present(self) -> "ValidationRule":
        if self.column_id is None and self.field is None:
            raise ValueError("validation rule requires column_id or field")
        return self


SheetMode = Literal["rows", "aggregate", "errors", "parameters"]
PostProcessOp = Literal["tomoru_phones"]


class SheetPostProcess(BaseModel):
    model_config = {"extra": "forbid"}
    op: PostProcessOp
    deal_alias: str = Field(default="deal", max_length=32, pattern=ALIAS_PATTERN)
    include_company_contacts: bool = True
    include_company_phones: bool = True
    fetch_company_contacts_live: bool = True
    deduplicate_phones: bool = True
    exclude_archived: bool = True
    use_llm_for_lpr: bool = True
    category_id: int = Field(default=15, ge=0)


class Sheet(BaseModel):
    model_config = {"extra": "forbid"}
    id: str = Field(min_length=1, max_length=64, pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=31)
    mode: SheetMode = "rows"
    dataset_id: str | None = Field(default=None, max_length=64)
    row_filters: list[Filter] = Field(default_factory=list, max_length=40)
    columns: list[Column] = Field(default_factory=list, max_length=200)
    sort: list[SortKey] = Field(default_factory=list, max_length=6)
    group_by: list[FieldRef] = Field(default_factory=list, max_length=10)
    aggregates: list[Aggregate] = Field(default_factory=list, max_length=20)
    validation_rules: list[ValidationRule] = Field(default_factory=list, max_length=30)
    error_policy: Literal["route_to_errors", "stop", "valid_only", "warn"] = "route_to_errors"
    post_process: SheetPostProcess | None = None

    @model_validator(mode="after")
    def _mode_requirements(self) -> "Sheet":
        if self.mode in ("rows", "aggregate", "errors") and not self.dataset_id:
            raise ValueError(f"sheet mode {self.mode} requires dataset_id")
        if self.mode == "aggregate" and not self.group_by and not self.aggregates:
            raise ValueError("aggregate sheet requires group_by or aggregates")
        ids = [c.id for c in self.columns]
        if len(ids) != len(set(ids)):
            raise ValueError("column ids must be unique within a sheet")
        return self


class Workbook(BaseModel):
    model_config = {"extra": "forbid"}
    format: Literal["xlsx", "csv"] = "xlsx"
    filename_label: str | None = Field(default="crm_export", max_length=60)
    include_params_sheet: bool = True
    include_errors_sheet: bool = True
    sheets: list[Sheet] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _unique_sheet_ids(self) -> "Workbook":
        ids = [s.id for s in self.sheets]
        if len(ids) != len(set(ids)):
            raise ValueError("sheet ids must be unique")
        return self


class MemoryRef(BaseModel):
    model_config = {"extra": "forbid"}
    memory_id: int
    kind: Literal["term", "alias", "mapping", "template", "rule", "instruction", "preference"]
    version: int | None = None
    hash: str | None = Field(default=None, max_length=64)


class ExportPlan2(BaseModel):
    model_config = {"extra": "forbid"}
    schema_version: Literal["2.0"] = "2.0"
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    datasets: list[Dataset] = Field(min_length=1, max_length=10)
    workbook: Workbook
    memory_refs: list[MemoryRef] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def _unique_dataset_ids(self) -> "ExportPlan2":
        ids = [d.id for d in self.datasets]
        if len(ids) != len(set(ids)):
            raise ValueError("dataset ids must be unique")
        # default source_alias on field refs using primary source where omitted
        primary_alias_by_dataset = {
            d.id: next((s.alias for s in d.sources if s.entity_type_id == d.primary_entity_type_id), d.sources[0].alias)
            for d in self.datasets
        }
        dataset_by_id = {d.id: d for d in self.datasets}
        for sheet in self.workbook.sheets:
            if not sheet.dataset_id or sheet.dataset_id not in dataset_by_id:
                continue
            default_alias = primary_alias_by_dataset[sheet.dataset_id]
            _fill_aliases_for_sheet(sheet, default_alias)
        return self


def _fill_aliases_for_sheet(sheet: Sheet, default_alias: str) -> None:
    def fill(ref: FieldRef | None) -> None:
        if ref is not None and ref.source_alias is None:
            ref.source_alias = default_alias

    for col in sheet.columns:
        _fill_value_aliases(col.value, fill)
    for f in sheet.row_filters:
        fill(f.field)
    for s in sheet.sort:
        fill(s.field)
    for g in sheet.group_by:
        fill(g)
    for agg in sheet.aggregates:
        fill(agg.field)
    for rule in sheet.validation_rules:
        fill(rule.field)


def _fill_value_aliases(value: Any, fill) -> None:
    kind = getattr(value, "kind", None)
    if kind == "field":
        fill(value.field)
    elif kind == "aggregate":
        fill(value.field)
    elif kind in ("concat", "coalesce"):
        for part in value.parts:
            _fill_value_aliases(part, fill)
    elif kind == "conditional":
        for case in value.cases:
            fill(case.when.field)
            _fill_value_aliases(case.then, fill)
        if value.default is not None:
            _fill_value_aliases(value.default, fill)
