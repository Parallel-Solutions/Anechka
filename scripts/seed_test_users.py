"""Create RBAC test users for intelligent export manual testing.

Idempotent: skips users that already exist for the current portal.
Run: docker compose exec web python scripts/seed_test_users.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import AppUser
from app.services.auth_service import AuthService, resolve_portal_id

# Local-only credentials for manual QA (not for production).
TEST_USERS = (
    {
        "email": "admin@local.test",
        "password": "AdminTest!2026",
        "role": "admin",
        "display_name": "Test Admin",
    },
    {
        "email": "analyst@local.test",
        "password": "AnalystTest!2026",
        "role": "analyst",
        "display_name": "Test Analyst",
    },
    {
        "email": "viewer@local.test",
        "password": "ViewerTest!2026",
        "role": "viewer",
        "display_name": "Test Viewer",
        "crm_user_external_id": 1,
    },
)


def main() -> int:
    settings = get_settings()
    portal_id = resolve_portal_id(settings)
    db = SessionLocal()
    try:
        auth = AuthService(settings, db)
        created: list[str] = []
        skipped: list[str] = []
        for spec in TEST_USERS:
            email = spec["email"].strip().lower()
            existing = db.scalar(
                select(AppUser).where(AppUser.portal_id == portal_id, AppUser.email == email)
            )
            if existing is not None:
                skipped.append(email)
                continue
            auth.create_user(
                email=email,
                password=spec["password"],
                role=spec["role"],
                display_name=spec["display_name"],
                crm_user_external_id=spec.get("crm_user_external_id"),
            )
            created.append(f"{email} ({spec['role']})")
    finally:
        db.close()

    print(f"Portal: {portal_id}")
    if created:
        print("Created:")
        for line in created:
            print(f"  - {line}")
    if skipped:
        print("Skipped (already exist):")
        for line in skipped:
            print(f"  - {line}")
    if not created and not skipped:
        print("No users processed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
