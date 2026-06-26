"""ExportPlan → SQLAlchemy query compiler (local PostgreSQL only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, and_, cast, func, or_, select, String
from sqlalchemy.orm import Session

from app.models import CrmEntity
from app.services.export_plan.catalog import DENORM_FIELD_MAP, FieldCatalog
from app.services.export_plan.models import ExportPlan, FieldRef, Filter, SortKey, Source


@dataclass
class CompiledSource:
    alias: str
    entity_type_id: int
    statement: Select[Any]


@dataclass
class CompiledQuery:
    plan: ExportPlan
    sources: dict[str, CompiledSource] = field(default_factory=dict)
    primary_alias: str = ""

    def count_statement(self) -> Select[Any]:
        primary = self.sources[self.primary_alias].statement
        return select(func.count()).select_from(primary.subquery())

    def fetch_statement(self, offset: int = 0, limit: int | None = None) -> Select[Any]:
        primary = self.sources[self.primary_alias].statement
        stmt = primary
        if limit is not None:
            stmt = stmt.limit(limit)
        if offset:
            stmt = stmt.offset(offset)
        return stmt


class ExportPlanCompiler:
    """Builds parameterized SQLAlchemy queries — never executes AI-provided SQL strings."""

    def __init__(self, db: Session, portal_id: str, catalog: FieldCatalog):
        self.db = db
        self.portal_id = portal_id
        self.catalog = catalog

    def compile(self, plan: ExportPlan) -> CompiledQuery:
        compiled = CompiledQuery(plan=plan)
        for source in plan.sources:
            stmt = self._compile_source(source)
            compiled.sources[source.alias] = CompiledSource(
                alias=source.alias,
                entity_type_id=source.entity_type_id,
                statement=stmt,
            )
        compiled.primary_alias = plan.sources[0].alias
        return compiled

    def _compile_source(self, source: Source) -> Select[Any]:
        stmt: Select[Any] = select(CrmEntity).where(
            CrmEntity.portal_id == self.portal_id,
            CrmEntity.entity_type_id == source.entity_type_id,
        )
        if not source.include_deleted:
            stmt = stmt.where(CrmEntity.is_deleted.is_(False))

        for filt in source.filters:
            stmt = stmt.where(self._compile_filter(filt))

        if plan_sort := self._sort_for_source(source):
            stmt = stmt.order_by(*plan_sort)

        if source.limit:
            stmt = stmt.limit(source.limit)

        return stmt

    def _sort_for_source(self, source: Source) -> list[Any]:
        return []

    def apply_plan_sort(self, stmt: Select[Any], plan: ExportPlan) -> Select[Any]:
        orders: list[Any] = []
        for key in plan.sort:
            expr = self._field_expression(key.field)
            orders.append(expr.asc() if key.direction == "asc" else expr.desc())
        if orders:
            return stmt.order_by(*orders)
        return stmt

    def _compile_filter(self, filt: Filter) -> Any:
        expr = self._field_expression(filt.field)
        op = filt.op
        if op == "eq":
            return expr == filt.value
        if op == "ne":
            return expr != filt.value
        if op == "gt":
            return expr > filt.value
        if op == "gte":
            return expr >= filt.value
        if op == "lt":
            return expr < filt.value
        if op == "lte":
            return expr <= filt.value
        if op == "in":
            return expr.in_(filt.values or [])
        if op == "not_in":
            return expr.notin_(filt.values or [])
        if op == "contains":
            return cast(expr, String).contains(str(filt.value))
        if op == "starts_with":
            return cast(expr, String).startswith(str(filt.value))
        if op == "is_null":
            return or_(expr.is_(None), cast(expr, String) == "")
        if op == "is_not_null":
            return and_(expr.isnot(None), cast(expr, String) != "")
        raise ValueError(f"Unsupported filter op: {op}")

    def _field_expression(self, ref: FieldRef) -> Any:
        code = ref.field_code.upper()
        entry = self.catalog.get(ref.entity_type_id, code)
        if entry and entry.storage == "column" and entry.column_name:
            return getattr(CrmEntity, entry.column_name)
        json_key = code
        if code == "ID":
            json_key = "id"
        return cast(CrmEntity.raw_payload[json_key], String)

    def count(self, compiled: CompiledQuery) -> int:
        return self.db.scalar(compiled.count_statement()) or 0

    def fetch_page(
        self,
        compiled: CompiledQuery,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> list[CrmEntity]:
        stmt = compiled.fetch_statement(offset=offset, limit=limit)
        return list(self.db.scalars(stmt))
