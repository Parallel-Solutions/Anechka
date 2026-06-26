"""Truncate CRM import tables and sync history; preserve app settings and export jobs.

Run: docker compose exec web python scripts/clear_crm_import_data.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `python scripts/clear_crm_import_data.py` from bitrix_export_web/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.database import SessionLocal

CRM_AND_SYNC_TABLES = [
    "sync_runs",
    "sync_checkpoints",
    "crm_contact_links",
    "crm_contact_phones",
    "crm_contacts",
    "crm_child_records",
    "crm_entity_field_values",
    "crm_entity_versions",
    "crm_entities",
    "crm_field_semantics",
    "crm_field_definition_versions",
    "crm_field_definitions",
    "crm_dictionary_entries",
    "crm_dictionaries",
    "crm_files",
    "crm_users",
    "crm_currencies",
]

PRESERVED_TABLES = [
    "app_settings",
    "export_jobs",
    "ai_prompt_templates",
]


def main() -> int:
    db = SessionLocal()
    try:
        table_list = ", ".join(CRM_AND_SYNC_TABLES)
        db.execute(
            text(f"TRUNCATE TABLE {table_list} RESTART IDENTITY CASCADE")
        )
        db.commit()
        print(f"Cleared {len(CRM_AND_SYNC_TABLES)} tables:")
        for name in CRM_AND_SYNC_TABLES:
            print(f"  - {name}")
        print("Preserved (not truncated):")
        for name in PRESERVED_TABLES:
            print(f"  - {name}")
        return 0
    except Exception as exc:
        db.rollback()
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
