"""One-way adapter ExportPlan v1.0 -> ExportPlan 2.0.

No v1 plans are persisted yet, but legacy plans (or AI output that still uses
the v1 shape) are upgraded to the 2.0 contract so the rest of the pipeline only
ever deals with 2.0. The adapter is intentionally conservative: anything it
cannot map safely is dropped and the resulting plan is re-validated by callers.
"""

from __future__ import annotations

import re

from app.services.export_plan.models import ExportPlan as ExportPlanV1
from app.services.export_plan.models_v2 import (
    Column,
    Dataset,
    ExportPlan2,
    FieldRef,
    FieldValue,
    Filter,
    RelationRef,
    Sheet,
    Source,
    TransformStep,
    Workbook,
)
from app.services.export_plan.registry import RELATIONS, TRANSFORMS

# v1 transform op -> (v2 op, param transformer)
_TRANSFORM_OP_MAP = {
    "trim": "trim",
    "uppercase": "uppercase",
    "lowercase": "lowercase",
    "phone_normalize": "phone_normalize",
    "phone_format_display": "phone_normalize",
    "date_format": "date_format",
    "number_format": "number_round",
    "dictionary_label": "dictionary_label",
    "mapping_lookup": "mapping_lookup",
    "default_value": "default_value",
}


def _slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_")
    if not s or not s[0].isalpha():
        s = f"{fallback}_{s}" if s else fallback
    return s[:60]


def adapt_v1_to_v2(plan: ExportPlanV1) -> ExportPlan2:
    primary = plan.sources[0]
    alias_types = {s.alias: s.entity_type_id for s in plan.sources}

    sources = [Source(alias=s.alias, entity_type_id=s.entity_type_id) for s in plan.sources]

    relation_refs: list[RelationRef] = []
    for join in plan.joins:
        from_type = alias_types.get(join.from_alias)
        to_type = alias_types.get(join.to_alias)
        match = next(
            (r for r in RELATIONS.values() if r.from_entity_type_id == from_type and r.to_entity_type_id == to_type),
            None,
        )
        if match:
            relation_refs.append(
                RelationRef(relation_code=match.relation_code, from_alias=join.from_alias, to_alias=join.to_alias)
            )

    filters: list[Filter] = []
    for src in plan.sources:
        for f in src.filters:
            filters.append(
                Filter(
                    field=FieldRef(
                        entity_type_id=f.field.entity_type_id,
                        field_code=f.field.field_code,
                        source_alias=f.field.source_alias or src.alias,
                    ),
                    op=f.op,
                    value=f.value,
                    values=f.values,
                )
            )

    dataset = Dataset(
        id="main",
        primary_entity_type_id=primary.entity_type_id,
        sources=sources,
        relation_refs=relation_refs,
        filters=filters,
        limit=max((s.limit for s in plan.sources if s.limit), default=5000),
        include_deleted=any(s.include_deleted for s in plan.sources),
    )

    transform_by_id = {t.id: t for t in plan.transforms}
    out_sheets = plan.output.sheets or []
    sheets: list[Sheet] = []
    used_ids: set[str] = set()

    for s_idx, out_sheet in enumerate(out_sheets):
        sheet_columns: list[Column] = []
        col_used: set[str] = set()
        for c_idx, col in enumerate(plan.columns):
            target = col.sheet or (out_sheets[0].name if out_sheets else None)
            if target != out_sheet.name:
                continue
            transforms = _map_transforms(col.transform_id, transform_by_id)
            cid = _unique(_slug(col.header, f"col{c_idx}"), col_used)
            sheet_columns.append(
                Column(
                    id=cid,
                    header=col.header,
                    value=FieldValue(
                        field=FieldRef(
                            entity_type_id=col.field.entity_type_id,
                            field_code=col.field.field_code,
                            source_alias=col.field.source_alias or primary.alias,
                        )
                    ),
                    transforms=transforms,
                    width=col.width,
                )
            )
        sid = _unique(_slug(out_sheet.name, f"sheet{s_idx}"), used_ids)
        sheets.append(
            Sheet(
                id=sid,
                name=out_sheet.name,
                mode="rows",
                dataset_id="main",
                columns=sheet_columns,
            )
        )

    if not sheets:
        sheets.append(Sheet(id="sheet1", name="Данные", mode="rows", dataset_id="main", columns=[]))

    workbook = Workbook(
        format=plan.output.format,
        filename_label=plan.output.filename_label or "crm_export",
        include_params_sheet=plan.output.include_params_sheet,
        include_errors_sheet=any(s.include_errors for s in out_sheets),
        sheets=sheets,
    )

    return ExportPlan2(
        schema_version="2.0",
        title=plan.title,
        description=plan.description,
        datasets=[dataset],
        workbook=workbook,
        memory_refs=[],
    )


def _map_transforms(transform_id: str | None, transform_by_id: dict) -> list[TransformStep]:
    if not transform_id or transform_id not in transform_by_id:
        return []
    t = transform_by_id[transform_id]
    v2_op = _TRANSFORM_OP_MAP.get(t.op)
    if v2_op is None or v2_op not in TRANSFORMS:
        return []
    params = dict(t.params or {})
    if v2_op == "number_round" and "digits" not in params:
        params = {"digits": int(params.get("decimals", 2)) if isinstance(params.get("decimals"), int) else 2}
    # drop params that the strict v2 model would reject
    _, err = _safe_params(v2_op, params)
    if err:
        params = {}
    return [TransformStep(op=v2_op, params=params)]


def _safe_params(op: str, params: dict):
    from app.services.export_plan.registry import validate_transform_params

    return validate_transform_params(op, params)


def _unique(candidate: str, used: set[str]) -> str:
    base = candidate or "item"
    name = base
    i = 1
    while name in used:
        name = f"{base}_{i}"
        i += 1
    used.add(name)
    return name
