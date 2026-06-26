"""Build the server-enforced data scope for a user (ADR-001).

The scope is computed on the server from the app role and is NOT something the
AI or the plan can widen. Viewer is restricted to its own assigned records and
a whitelist of entity types, and cannot export sensitive fields.
"""

from __future__ import annotations

from app.config import Settings
from app.models import AppUser, ENTITY_CONTACT, ENTITY_DEAL
from app.services.export_plan.validator import ExportScope

VIEWER_ENTITY_WHITELIST = frozenset({ENTITY_DEAL, ENTITY_CONTACT})
VIEWER_MAX_ROWS = 1000


def build_scope(user: AppUser, settings: Settings) -> ExportScope:
    if user.role == "viewer":
        return ExportScope(
            role="viewer",
            allowed_entity_type_ids=VIEWER_ENTITY_WHITELIST,
            assigned_by_id=user.crm_user_external_id,
            max_rows=min(VIEWER_MAX_ROWS, settings.ie_max_export_rows),
            allow_sensitive_fields=False,
        )
    # analyst / admin
    return ExportScope(
        role=user.role,
        allowed_entity_type_ids=None,
        assigned_by_id=None,
        max_rows=settings.ie_max_export_rows,
        allow_sensitive_fields=True,
    )
