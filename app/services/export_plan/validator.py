"""ExportPlan server-side validation — no trust in AI output."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.export_plan.catalog import FieldCatalog, SENSITIVE_FIELD_CODES
from app.services.export_plan.models import ExportPlan, FieldRef, Filter, Source


@dataclass
class ValidationIssue:
    code: str
    message: str
    path: str
    severity: str = "error"


@dataclass
class ValidationResult:
    valid: bool
    issues: list[ValidationIssue] = field(default_factory=list)

    def add(self, code: str, message: str, path: str, severity: str = "error") -> None:
        self.issues.append(ValidationIssue(code=code, message=message, path=path, severity=severity))
        if severity == "error":
            self.valid = False


@dataclass
class ExportScope:
    """App-level data scope (ADR-001) — applied during validation."""

    role: str = "admin"
    allowed_entity_type_ids: frozenset[int] | None = None
    assigned_by_id: int | None = None
    max_rows: int = 5000
    allow_sensitive_fields: bool = True


class ExportPlanValidator:
    ALLOWED_FILTER_OPS = frozenset(
        {"eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains", "starts_with", "is_null", "is_not_null"}
    )
    ALLOWED_TRANSFORM_OPS = frozenset(
        {
            "none", "trim", "uppercase", "lowercase", "phone_normalize", "phone_format_display",
            "date_format", "number_format", "dictionary_label", "mapping_lookup", "default_value",
        }
    )

    def __init__(self, catalog: FieldCatalog, scope: ExportScope | None = None):
        self.catalog = catalog
        self.scope = scope or ExportScope()

    def validate(self, plan: ExportPlan) -> ValidationResult:
        result = ValidationResult(valid=True)
        source_aliases = {s.alias for s in plan.sources}

        for idx, source in enumerate(plan.sources):
            self._validate_source(source, idx, result)
            if self.scope.allowed_entity_type_ids and source.entity_type_id not in self.scope.allowed_entity_type_ids:
                result.add(
                    "ENTITY_TYPE_DENIED",
                    f"Entity type {source.entity_type_id} not allowed for role {self.scope.role}",
                    f"sources[{idx}].entity_type_id",
                )

        for idx, join in enumerate(plan.joins):
            if join.from_alias not in source_aliases or join.to_alias not in source_aliases:
                result.add("JOIN_ALIAS_UNKNOWN", "Join references unknown source alias", f"joins[{idx}]")
            self._validate_field_ref(join.from_field, f"joins[{idx}].from_field", source_aliases, result)
            self._validate_field_ref(join.to_field, f"joins[{idx}].to_field", source_aliases, result)

        transform_ids = {t.id for t in plan.transforms}
        for t in plan.transforms:
            if t.op not in self.ALLOWED_TRANSFORM_OPS:
                result.add("TRANSFORM_OP_UNKNOWN", f"Unknown transform op: {t.op}", f"transforms.{t.id}")

        if not plan.columns:
            result.add("COLUMNS_EMPTY", "At least one column is required", "columns")

        for idx, col in enumerate(plan.columns):
            self._validate_field_ref(col.field, f"columns[{idx}].field", source_aliases, result)
            if col.transform_id and col.transform_id not in transform_ids:
                result.add(
                    "TRANSFORM_REF_MISSING",
                    f"Unknown transform_id: {col.transform_id}",
                    f"columns[{idx}].transform_id",
                )

        for idx, rule in enumerate(plan.validation_rules):
            self._validate_field_ref(rule.field, f"validation_rules[{idx}].field", source_aliases, result)
            if rule.type == "regex" and not rule.params.get("pattern"):
                result.add("REGEX_PATTERN_MISSING", "regex rule requires params.pattern", f"validation_rules[{idx}]")

        total_limit = self._effective_limit(plan)
        if total_limit > self.scope.max_rows:
            result.add(
                "LIMIT_EXCEEDED",
                f"Plan limit {total_limit} exceeds max_rows {self.scope.max_rows}",
                "sources",
            )

        if self.scope.role == "viewer" and plan.output.format == "xlsx":
            for sheet in plan.output.sheets:
                if not sheet.include_errors and len(plan.sources) > 0:
                    pass  # viewer can preview

        return result

    def _validate_source(self, source: Source, idx: int, result: ValidationResult) -> None:
        path = f"sources[{idx}]"
        if source.limit and source.limit > self.scope.max_rows:
            result.add("SOURCE_LIMIT", f"Source limit exceeds max_rows", f"{path}.limit")

        if self.scope.assigned_by_id is not None and self.scope.role == "viewer":
            has_scope = any(
                f.field.field_code.upper() == "ASSIGNED_BY_ID"
                and f.op == "eq"
                and f.value == self.scope.assigned_by_id
                for f in source.filters
            )
            if not has_scope:
                result.add(
                    "SCOPE_ASSIGNED_REQUIRED",
                    "Viewer must filter ASSIGNED_BY_ID to own crm user id",
                    f"{path}.filters",
                )

        for fidx, filt in enumerate(source.filters):
            self._validate_filter(filt, f"{path}.filters[{fidx}]", result)

    def _validate_filter(self, filt: Filter, path: str, result: ValidationResult) -> None:
        if filt.op not in self.ALLOWED_FILTER_OPS:
            result.add("FILTER_OP_UNKNOWN", f"Unknown filter op: {filt.op}", path)
        self._validate_field_ref(filt.field, f"{path}.field", None, result)

    def _validate_field_ref(
        self,
        ref: FieldRef,
        path: str,
        source_aliases: set[str] | None,
        result: ValidationResult,
    ) -> None:
        code = ref.field_code.upper()
        if not self.catalog.is_allowed(ref.entity_type_id, code):
            result.add(
                "FIELD_NOT_IN_CATALOG",
                f"Field {code} not in catalog for entity_type_id={ref.entity_type_id}",
                path,
            )
        if not self.scope.allow_sensitive_fields and code in SENSITIVE_FIELD_CODES:
            result.add("FIELD_SENSITIVE_DENIED", f"Field {code} is not exportable for this role", path)
        if ref.source_alias and source_aliases is not None and ref.source_alias not in source_aliases:
            result.add("SOURCE_ALIAS_UNKNOWN", f"Unknown source_alias: {ref.source_alias}", path)

    @staticmethod
    def _effective_limit(plan: ExportPlan) -> int:
        limits = [s.limit for s in plan.sources if s.limit]
        if limits:
            return max(limits)
        return 5000
