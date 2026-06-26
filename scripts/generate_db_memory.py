"""Generate db-aware *proposed* planner memory from the current portal data.

Read-only profiling; nothing is auto-approved. Idempotent on re-run.

Run: docker compose exec web python scripts/generate_db_memory.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `python scripts/generate_db_memory.py` from bitrix_export_web/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.database import SessionLocal
from app.repositories.intelligent_export_repository import IntelligentExportRepository, ScopeContext
from app.services.auth_service import AuthService, resolve_portal_id
from app.services.intelligent_export.memory_generator import generate_memory


def main() -> int:
    settings = get_settings()
    portal_id = resolve_portal_id(settings)
    db = SessionLocal()
    try:
        user = AuthService(settings, db).get_default_ie_user()
        repo = IntelligentExportRepository(db, ScopeContext(user=user, portal_id=portal_id))
        result = generate_memory(db, repo, portal_id, settings)
        print(f"Portal: {portal_id}")
        print(f"  created: {len(result['created'])}")
        print(f"  updated: {len(result['updated'])}")
        print(f"  skipped: {len(result['skipped'])}")
        for entry in result["created"] + result["updated"]:
            print(f"    [{entry.status}] {entry.kind} {entry.key} (p{entry.priority})")
        return 0
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
