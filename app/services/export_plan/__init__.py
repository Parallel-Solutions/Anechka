"""ExportPlan domain — structured export plans (no arbitrary SQL from AI)."""

from app.services.export_plan.adapter import adapt_v1_to_v2
from app.services.export_plan.catalog import FieldCatalog, FieldCatalogEntry
from app.services.export_plan.compiler import CompiledQuery, ExportPlanCompiler
from app.services.export_plan.models import ExportPlan
from app.services.export_plan.models_v2 import ExportPlan2
from app.services.export_plan.validator import ExportPlanValidator, ExportScope, ValidationResult
from app.services.export_plan.validator_v2 import validate_structure

__all__ = [
    "ExportPlan",
    "ExportPlan2",
    "ExportPlanCompiler",
    "ExportPlanValidator",
    "ExportScope",
    "ValidationResult",
    "FieldCatalog",
    "FieldCatalogEntry",
    "CompiledQuery",
    "adapt_v1_to_v2",
    "validate_structure",
]
