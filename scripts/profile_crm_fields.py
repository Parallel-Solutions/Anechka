"""Profile CRM field values from raw_payload and optionally run AI semantics.

Run:
  docker compose exec web python scripts/profile_crm_fields.py
  docker compose exec web python scripts/profile_crm_fields.py --no-ai
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select

from app.config import merge_db_settings
from app.database import SessionLocal
from app.models import CrmFieldDefinition, CrmFieldValueProfile
from app.repositories.crm_repository import CrmRepository
from app.services.auth_service import resolve_portal_id
from app.services.bitrix_import.field_value_profiler import FieldValueProfiler
from app.services.bitrix_import.metadata_ai_service import BitrixMetadataAIService
from app.services.settings_service import load_settings_from_db


def _print_stats(db, portal_id: str, crm_repo: CrmRepository) -> None:
    profiles_with_data = db.scalar(
        select(func.count())
        .select_from(CrmFieldValueProfile)
        .where(
            CrmFieldValueProfile.portal_id == portal_id,
            CrmFieldValueProfile.filled_count > 0,
        )
    ) or 0
    fields = list(
        db.scalars(
            select(CrmFieldDefinition).where(
                CrmFieldDefinition.portal_id == portal_id,
                CrmFieldDefinition.is_active.is_(True),
            )
        )
    )
    without_semantic = 0
    needs_review = 0
    for f in fields:
        sem = crm_repo.get_semantic(f.id)
        if sem is None:
            without_semantic += 1
        elif sem.needs_review:
            needs_review += 1
    print(f"  profiles with data: {profiles_with_data}")
    print(f"  active fields: {len(fields)}")
    print(f"  without semantic: {without_semantic}")
    print(f"  needs review: {needs_review}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan CRM raw_payload and generate field semantics")
    parser.add_argument("--no-ai", action="store_true", help="Only profile values, skip OpenAI analysis")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        settings = merge_db_settings(load_settings_from_db(db))
        portal_id = resolve_portal_id(settings)
        print(f"Portal: {portal_id}")

        profiler = FieldValueProfiler(db, portal_id)
        profile_count = profiler.profile_all()
        db.commit()
        print(f"Profiles updated: {profile_count}")

        crm_repo = CrmRepository(db, portal_id)
        ai_processed = 0
        if not args.no_ai:
            if not settings.openai_api_key:
                print("Warning: OPENAI_API_KEY not configured, skipping AI analysis")
            else:
                fields = list(
                    db.scalars(
                        select(CrmFieldDefinition).where(
                            CrmFieldDefinition.portal_id == portal_id,
                            CrmFieldDefinition.is_active.is_(True),
                        )
                    )
                )
                profiles = crm_repo.get_value_profiles_by_field_ids([f.id for f in fields])
                ai = BitrixMetadataAIService(settings, db, portal_id)
                ai_processed = ai.analyze_fields(fields, value_profiles=profiles, force=True)
                db.commit()
                print(f"AI fields processed: {ai_processed}")
                print(f"AI requests: {ai.ai_requests_count}")

        print("Summary:")
        _print_stats(db, portal_id, crm_repo)
        return 0
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
