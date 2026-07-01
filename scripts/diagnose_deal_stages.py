"""Compare deal stage_id in local DB vs live Bitrix API."""

from __future__ import annotations

import argparse
import sys

from app.database import SessionLocal
from app.dependencies import get_app_settings
from app.models import CrmEntity, ENTITY_DEAL
from app.repositories.sync_repository import SyncRepository
from app.services.bitrix_client import BitrixClient
from app.utils.portal import portal_id_from_webhook
from sqlalchemy import select


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose deal stage discrepancies")
    parser.add_argument("deal_ids", nargs="+", type=int, help="Bitrix deal entity IDs")
    parser.add_argument("--category-id", type=int, default=15)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        settings = get_app_settings(db)
        portal_id = portal_id_from_webhook(settings.bitrix_webhook_url)
        client = BitrixClient(settings)
        sync_repo = SyncRepository(db)
        cp = sync_repo.get_checkpoint(portal_id, "entities", ENTITY_DEAL)
        sync_at = cp.last_successful_sync_at if cp else None

        stages = client.get_stages(args.category_id)
        stage_names = {s["id"]: s["name"] for s in stages}
        archive_ids = {sid for sid, name in stage_names.items() if "архив" in name.lower()}

        print(f"Portal: {portal_id}")
        print(f"Last sync: {sync_at}")
        print(f"Archive stage IDs: {sorted(archive_ids)}")
        print()
        print(f"{'ID':<8} {'DB stage':<18} {'Bitrix stage':<18} {'Bitrix name':<20} {'Match':<6}")
        print("-" * 80)

        mismatches = 0
        for deal_id in args.deal_ids:
            entity = db.scalar(
                select(CrmEntity).where(
                    CrmEntity.portal_id == portal_id,
                    CrmEntity.entity_type_id == ENTITY_DEAL,
                    CrmEntity.entity_id == deal_id,
                )
            )
            db_stage = entity.stage_id if entity else None

            resp = client.call("crm.item.get", {"entityTypeId": ENTITY_DEAL, "id": deal_id})
            item = resp.get("result", {}).get("item") or resp.get("result") or {}
            bx_stage = item.get("stageId") or item.get("STAGE_ID")
            bx_name = stage_names.get(str(bx_stage), "?")
            match = db_stage == bx_stage
            if not match:
                mismatches += 1
            print(
                f"{deal_id:<8} {db_stage or '-':<18} {bx_stage or '-':<18} {bx_name:<20} {'yes' if match else 'NO':<6}"
            )

        return 1 if mismatches else 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
