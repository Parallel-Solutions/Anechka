"""ExportPlan 2.0 -> SQLAlchemy query compiler (local PostgreSQL only).

Hard guarantees:
- no ``text()`` with user/AI values, no raw SQL, no eval/exec;
- table is always ``crm_entities`` (aliased per source) — never dynamic;
- columns/JSONB keys come from the field code only (whitelisted), never a
  generic JSONPath supplied by AI;
- portal_id is always applied; is_deleted excluded by default;
- user data scope (viewer assigned-only) applied independently of the plan;
- JOINs use only approved relations from the server registry;
- bind parameters are used for all values.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, Integer, Numeric, Select, String, Text, and_, cast, func, or_, select, text, true
from sqlalchemy.orm import Session, aliased

from app.models import CrmContactLink, CrmEntity
from app.services.export_plan.catalog import FieldCatalog
from app.services.export_plan.models_v2 import Condition, Dataset, FieldRef
from app.services.export_plan.payload_keys import camel_key
from app.services.export_plan.registry import get_relation
from app.services.export_plan.validator import ExportScope

logger = logging.getLogger(__name__)

# CRM relation foreign keys stored in raw_payload (not denormalized columns).
_RELATION_FK_FIELDS = frozenset({"CONTACT_ID", "COMPANY_ID", "LEAD_ID", "DEAL_ID"})

_DATETIME_FIELD_CODES = frozenset({"DATE_CREATE", "DATE_MODIFY", "UPDATED_TIME", "CREATED_TIME"})
_DATE_FIELD_CODES = frozenset({"CLOSEDATE", "CLOSED_AT"})


def _parse_filter_datetime(value: Any) -> datetime | date | Any:
    """Parse ISO date/datetime strings for timestamptz column comparisons."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        try:
            d = date.fromisoformat(text)
            return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        except ValueError:
            return value
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class CompileError(Exception):
    code = "PLAN_INVALID"


@dataclass
class CompiledDataset:
    dataset_id: str
    primary_alias: str
    alias_order: list[str]
    alias_entities: dict[str, Any]
    base_select: Select[Any]

    def count_statement(self) -> Select[Any]:
        return select(func.count()).select_from(self.base_select.subquery())

    def page_statement(self, offset: int, limit: int) -> Select[Any]:
        stmt = self.base_select
        if limit is not None:
            stmt = stmt.limit(limit)
        if offset:
            stmt = stmt.offset(offset)
        return stmt


def apply_statement_timeout(db: Session, ms: int) -> None:
    """Best-effort PostgreSQL statement timeout; no-op on SQLite."""
    if db.bind is None or db.bind.dialect.name != "postgresql" or ms <= 0:
        return
    safe_ms = int(ms)
    try:
        # PostgreSQL SET does not accept bind parameters for the value.
        db.execute(text(f"SET LOCAL statement_timeout = '{safe_ms}ms'"))
    except Exception:  # noqa: BLE001
        logger.warning("Failed to set statement_timeout=%sms", safe_ms, exc_info=True)


class ExportPlanCompilerV2:
    def __init__(self, db: Session, portal_id: str, catalog: FieldCatalog, scope: ExportScope | None = None):
        self.db = db
        self.portal_id = portal_id
        self.catalog = catalog
        self.scope = scope or ExportScope()

    def compile_dataset(self, dataset: Dataset) -> CompiledDataset:
        alias_entities: dict[str, Any] = {}
        alias_type: dict[str, int] = {}
        for src in dataset.sources:
            alias_entities[src.alias] = aliased(CrmEntity, name=f"src_{src.alias}")
            alias_type[src.alias] = src.entity_type_id

        primary_alias = next(
            (s.alias for s in dataset.sources if s.entity_type_id == dataset.primary_entity_type_id),
            dataset.sources[0].alias,
        )
        alias_order = [primary_alias] + [a for a in alias_entities if a != primary_alias]

        primary_entity = alias_entities[primary_alias]
        stmt: Select[Any] = select(*[alias_entities[a] for a in alias_order])
        stmt = stmt.select_from(primary_entity)

        # JOINs from approved relations only
        joined: set[str] = {primary_alias}
        for rel in dataset.relation_refs:
            rel_def = get_relation(rel.relation_code)
            if rel_def is None:
                raise CompileError(f"unknown relation_code: {rel.relation_code}")
            if rel.from_alias not in alias_entities or rel.to_alias not in alias_entities:
                raise CompileError("relation references unknown alias")
            if rel.from_alias not in joined:
                raise CompileError("relation from_alias not connected to primary (no cartesian products)")
            if rel.to_alias in joined:
                raise CompileError("relation creates a cycle / duplicate join")
            from_e = alias_entities[rel.from_alias]
            to_e = alias_entities[rel.to_alias]
            join_isouter = rel_def.join_type != "inner"

            # Junction relations (Variant A): from (deal/lead) -> crm_contact_links
            # -> contact. parent_entity_id stores the Bitrix entity id; contact_id
            # may be negative/synthetic (lead backfill) with no crm_entities row,
            # which under a left join simply yields NULL contact columns.
            if rel_def.via_table == "crm_contact_links":
                link_alias = aliased(CrmContactLink, name=f"link_{rel.to_alias}")
                on_link = and_(
                    link_alias.portal_id == self.portal_id,
                    link_alias.parent_entity_type_id == rel_def.via_parent_type_id,
                    link_alias.parent_entity_id == from_e.entity_id,
                    link_alias.is_primary.is_(True) if rel_def.primary_only else true(),
                )
                stmt = stmt.join(link_alias, on_link, isouter=join_isouter)
                on_contact = and_(
                    to_e.portal_id == self.portal_id,
                    to_e.entity_type_id == alias_type[rel.to_alias],
                    to_e.entity_id == link_alias.contact_id,
                    to_e.is_deleted.is_(False),
                )
                stmt = stmt.join(to_e, on_contact, isouter=join_isouter)
                joined.add(rel.to_alias)
                continue

            from_expr = self._join_field_expr(from_e, rel_def.from_field_code)
            if rel_def.to_field_code.upper() == "ID":
                join_match = from_expr == to_e.entity_id
            else:
                to_expr = self._join_field_expr(to_e, rel_def.to_field_code)
                join_match = cast(from_expr, String) == cast(to_expr, String)
            on_clause = and_(
                join_match,
                to_e.portal_id == self.portal_id,
                to_e.entity_type_id == alias_type[rel.to_alias],
                to_e.is_deleted.is_(False),
            )
            stmt = stmt.join(to_e, on_clause, isouter=join_isouter)
            joined.add(rel.to_alias)

        # base scoping on primary
        stmt = stmt.where(
            primary_entity.portal_id == self.portal_id,
            primary_entity.entity_type_id == dataset.primary_entity_type_id,
        )
        if not dataset.include_deleted:
            stmt = stmt.where(primary_entity.is_deleted.is_(False))

        # data scope (independent of plan): viewer assigned-only
        if self.scope.role == "viewer" and self.scope.assigned_by_id is not None:
            stmt = stmt.where(primary_entity.assigned_by_id == self.scope.assigned_by_id)

        # dataset filters
        for filt in dataset.filters:
            stmt = stmt.where(self._compile_condition(filt, alias_entities))

        # dataset sort
        orders = []
        for sort in dataset.sort:
            expr = self._field_expr(sort.field, alias_entities)
            orders.append(expr.asc() if sort.direction == "asc" else expr.desc())
        if orders:
            stmt = stmt.order_by(*orders)
        else:
            stmt = stmt.order_by(primary_entity.id)

        return CompiledDataset(
            dataset_id=dataset.id,
            primary_alias=primary_alias,
            alias_order=alias_order,
            alias_entities=alias_entities,
            base_select=stmt,
        )

    # --- expressions --------------------------------------------------------
    def _join_field_expr(self, entity_alias: Any, field_code: str) -> Any:
        code = field_code.upper()
        if code == "ID":
            return entity_alias.entity_id
        from app.services.export_plan.catalog import DENORM_FIELD_MAP

        if code in DENORM_FIELD_MAP:
            return getattr(entity_alias, DENORM_FIELD_MAP[code])
        # raw_payload keys arrive camelCase from crm.item.list (contactId), but
        # plans/catalog use UPPER_SNAKE (CONTACT_ID). JSONB lookups are
        # case-sensitive, so coalesce both spellings.
        raw_value = func.coalesce(
            entity_alias.raw_payload[code].as_string(),
            entity_alias.raw_payload[camel_key(code)].as_string(),
        )
        if code in _RELATION_FK_FIELDS:
            return cast(raw_value, BigInteger)
        return cast(raw_value, String)

    def _field_expr(self, ref: FieldRef, alias_entities: dict[str, Any]) -> Any:
        alias = ref.source_alias
        if alias is None or alias not in alias_entities:
            raise CompileError(f"unknown source_alias for field {ref.field_code}")
        entity_alias = alias_entities[alias]
        code = ref.field_code.upper()
        entry = self.catalog.get(ref.entity_type_id, code)
        if entry is None:
            raise CompileError(f"field not in catalog: {code}")
        if entry.storage == "column" and entry.column_name:
            return getattr(entity_alias, entry.column_name)
        if code == "ID":
            return cast(entity_alias.raw_payload["id"].as_string(), String)
        # camelCase raw_payload keys (see _join_field_expr): coalesce both forms.
        return cast(
            func.coalesce(
                entity_alias.raw_payload[code].as_string(),
                entity_alias.raw_payload[camel_key(code)].as_string(),
            ),
            String,
        )

    def _coerce_filter_value(self, expr: Any, value: Any, *, field_code: str | None = None) -> Any:
        if value is None:
            return value
        code = (field_code or "").upper()
        if code == "STAGE_ID":
            return str(value)
        if code in _DATETIME_FIELD_CODES or code in _DATE_FIELD_CODES:
            coerced = _parse_filter_datetime(value)
            if isinstance(value, str) and not isinstance(coerced, (datetime, date)):
                raise CompileError(f"Некорректная дата для {code}: {value!r}")
            return coerced
        typ = expr.type
        python_type = getattr(typ, "python_type", None)
        if python_type is datetime or isinstance(typ, DateTime):
            coerced = _parse_filter_datetime(value)
            if isinstance(value, str) and not isinstance(coerced, (datetime, date)):
                raise CompileError(f"Некорректная дата для фильтра: {value!r}")
            return coerced
        if python_type is date or isinstance(typ, Date):
            parsed = _parse_filter_datetime(value)
            if isinstance(parsed, datetime):
                return parsed.date()
            if isinstance(parsed, date):
                return parsed
            return value
        if python_type is str or isinstance(typ, (String, Text)):
            return str(value)
        if python_type is int or isinstance(typ, (Integer, BigInteger)):
            return int(value)
        if python_type is float or isinstance(typ, (Numeric, Float)):
            return float(value)
        if python_type is bool or isinstance(typ, Boolean):
            return bool(value)
        return value

    def _coerce_filter_values(
        self,
        expr: Any,
        values: list[Any] | None,
        *,
        field_code: str | None = None,
    ) -> list[Any]:
        return [self._coerce_filter_value(expr, v, field_code=field_code) for v in (values or [])]

    def _compile_condition(self, cond: Condition, alias_entities: dict[str, Any]) -> Any:
        expr = self._field_expr(cond.field, alias_entities)
        field_code = cond.field.field_code.upper()
        op = cond.op
        if op == "eq" and field_code == "CATEGORY_ID" and cond.value == 15:
            deal_alias = cond.field.source_alias or next(iter(alias_entities))
            stage_expr = getattr(alias_entities[deal_alias], "stage_id")
            from app.services.intelligent_export.kp_legacy_stages import legacy_kp_stage_ids

            legacy_ids = legacy_kp_stage_ids(self.db, self.portal_id)
            return or_(
                expr == 15,
                and_(or_(expr.is_(None), expr == 0), stage_expr.in_(list(legacy_ids))),
            )
        from app.services.intelligent_export.contact_phone_heuristic import (
            REGION_TITLE_SYNONYMS,
            TOMORU_REGION_FIELD,
        )
        from app.services.intelligent_export.tomoru_regions import resolve_region_filter_value

        if op == "eq" and field_code == TOMORU_REGION_FIELD:
            from app.services.intelligent_export.tomoru_regions import (
                ORENBURG_REGION_SENTINEL,
                TOMORU_LEGACY_KP_REGION_FIELD,
                TOMORU_TITLE_REGIONS,
            )

            if cond.value == ORENBURG_REGION_SENTINEL:
                deal_alias = cond.field.source_alias or next(iter(alias_entities))
                title_expr = getattr(alias_entities[deal_alias], "title")
                orenburg = next(r for r in TOMORU_TITLE_REGIONS if r.key == "orenburg")
                legacy_ref = FieldRef(
                    entity_type_id=2,
                    field_code=TOMORU_LEGACY_KP_REGION_FIELD,
                    source_alias=deal_alias,
                )
                legacy_expr = self._field_expr(legacy_ref, alias_entities)
                return or_(
                    cast(title_expr, String).contains(orenburg.title_needle),
                    legacy_expr == str(orenburg.legacy_uf_id),
                )
            try:
                region_id = resolve_region_filter_value(cond.value)
            except ValueError as exc:
                raise CompileError(f"Не удалось определить ID региона: {cond.value!r}") from exc
            synonyms = REGION_TITLE_SYNONYMS.get(region_id, ())
            if synonyms:
                deal_alias = cond.field.source_alias or next(iter(alias_entities))
                title_expr = getattr(alias_entities[deal_alias], "title")
                uf_match = cast(expr, String) == str(region_id)
                title_clauses = [cast(title_expr, String).contains(s) for s in synonyms]
                return or_(uf_match, *title_clauses)
        if op == "eq":
            return expr == self._coerce_filter_value(expr, cond.value, field_code=field_code)
        if op == "ne":
            return expr != self._coerce_filter_value(expr, cond.value, field_code=field_code)
        if op == "gt":
            return expr > self._coerce_filter_value(expr, cond.value, field_code=field_code)
        if op == "gte":
            return expr >= self._coerce_filter_value(expr, cond.value, field_code=field_code)
        if op == "lt":
            return expr < self._coerce_filter_value(expr, cond.value, field_code=field_code)
        if op == "lte":
            return expr <= self._coerce_filter_value(expr, cond.value, field_code=field_code)
        if op == "in":
            return expr.in_(self._coerce_filter_values(expr, cond.values, field_code=field_code))
        if op == "not_in":
            return expr.notin_(self._coerce_filter_values(expr, cond.values, field_code=field_code))
        if op == "contains":
            return cast(expr, String).contains(str(cond.value))
        if op == "starts_with":
            return cast(expr, String).startswith(str(cond.value))
        if op == "is_null":
            return or_(expr.is_(None), cast(expr, String) == "")
        if op == "is_not_null":
            return and_(expr.isnot(None), cast(expr, String) != "")
        raise CompileError(f"unsupported op: {op}")

    # --- execution ----------------------------------------------------------
    def count(self, compiled: CompiledDataset, *, timeout_ms: int = 0) -> int:
        apply_statement_timeout(self.db, timeout_ms)
        return self.db.scalar(compiled.count_statement()) or 0

    def fetch_page(
        self,
        compiled: CompiledDataset,
        *,
        offset: int = 0,
        limit: int = 100,
        timeout_ms: int = 0,
    ) -> list[dict[str, Any]]:
        apply_statement_timeout(self.db, timeout_ms)
        rows = self.db.execute(compiled.page_statement(offset, limit)).all()
        out: list[dict[str, Any]] = []
        for row in rows:
            mapped: dict[str, Any] = {}
            for idx, alias in enumerate(compiled.alias_order):
                mapped[alias] = row[idx]
            out.append(mapped)
        return out
