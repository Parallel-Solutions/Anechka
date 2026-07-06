"""Build the server-enforced data scope for a user.

All app users currently share the same access level: full portal data.
"""

from __future__ import annotations

from app.config import Settings
from app.models import AppUser
from app.services.export_plan.validator import ExportScope


def build_scope(user: AppUser, settings: Settings) -> ExportScope:
    return ExportScope(
        role=user.role,
        allowed_entity_type_ids=None,
        assigned_by_id=None,
        max_rows=settings.ie_max_export_rows,
        allow_sensitive_fields=True,
    )
