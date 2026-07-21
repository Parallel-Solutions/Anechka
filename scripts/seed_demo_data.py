"""Seed or remove synthetic CRM and call-result data for local manual QA.

The dataset is idempotent and never calls Bitrix24, OpenAI, or Tomoru.

Usage:
    docker compose exec web python scripts/seed_demo_data.py
    docker compose exec web python scripts/seed_demo_data.py --clean
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    BitrixPreparedAction,
    CallContactSearchEntry,
    CallResultImport,
    CallResultImportRow,
    CallRetryQueueEntry,
    CrmContact,
    CrmContactLink,
    CrmContactPhone,
    CrmDictionary,
    CrmDictionaryEntry,
    CrmEntity,
    ENTITY_COMPANY,
    ENTITY_DEAL,
    SyncCheckpoint,
    SyncRun,
)
from app.repositories.contact_repository import ContactRepository
from app.services.auth_service import resolve_portal_id

DEMO_PREFIX = "DEMO-CODEX"
DEMO_MARKER = "demo-codex"
DEMO_DEAL_IDS = tuple(range(990001, 990056))
DEMO_CONTACT_IDS = tuple(range(991001, 991056))
DEMO_COMPANY_IDS = (992001, 992002)
REGIONS = (1091, 1105, 1107, 1069)
STAGES = (
    ("C15:NEW", "Новая"),
    ("C15:4", "Тёплый"),
    ("C15:UC_8W3UAD", "Архив"),
)


def _payload_hash(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _cleanup(db, portal_id: str) -> dict[str, int]:
    demo_imports = list(
        db.scalars(
            select(CallResultImport).where(
                CallResultImport.portal_id == portal_id,
                CallResultImport.campaign_name.like(f"{DEMO_PREFIX}%"),
            )
        )
    )
    import_ids = [item.id for item in demo_imports]
    if import_ids:
        db.execute(delete(CallRetryQueueEntry).where(CallRetryQueueEntry.import_id.in_(import_ids)))
        db.execute(delete(CallContactSearchEntry).where(CallContactSearchEntry.import_id.in_(import_ids)))
        for item in demo_imports:
            db.delete(item)

    db.execute(
        delete(CrmContactLink).where(
            CrmContactLink.portal_id == portal_id,
            CrmContactLink.contact_id.in_(DEMO_CONTACT_IDS),
        )
    )
    db.execute(
        delete(CrmContactPhone).where(
            CrmContactPhone.portal_id == portal_id,
            CrmContactPhone.contact_id.in_(DEMO_CONTACT_IDS),
        )
    )
    db.execute(
        delete(CrmContact).where(
            CrmContact.portal_id == portal_id,
            CrmContact.contact_id.in_(DEMO_CONTACT_IDS),
        )
    )
    db.execute(
        delete(CrmEntity).where(
            CrmEntity.portal_id == portal_id,
            CrmEntity.entity_type_id == ENTITY_DEAL,
            CrmEntity.entity_id.in_(DEMO_DEAL_IDS),
        )
    )
    db.execute(
        delete(CrmEntity).where(
            CrmEntity.portal_id == portal_id,
            CrmEntity.entity_type_id == ENTITY_COMPANY,
            CrmEntity.entity_id.in_(DEMO_COMPANY_IDS),
        )
    )

    demo_entries = list(
        db.scalars(select(CrmDictionaryEntry).where(CrmDictionaryEntry.source_hash == DEMO_MARKER))
    )
    for entry in demo_entries:
        db.delete(entry)
    demo_dicts = list(
        db.scalars(
            select(CrmDictionary).where(
                CrmDictionary.portal_id == portal_id,
                CrmDictionary.source_hash == DEMO_MARKER,
            )
        )
    )
    for dictionary in demo_dicts:
        db.delete(dictionary)

    demo_checkpoints = list(
        db.scalars(
            select(SyncCheckpoint).where(
                SyncCheckpoint.portal_id == portal_id,
                SyncCheckpoint.resource_name == "demo_codex",
            )
        )
    )
    for checkpoint in demo_checkpoints:
        db.delete(checkpoint)
    demo_runs = list(
        db.scalars(
            select(SyncRun).where(
                SyncRun.portal_id == portal_id,
                SyncRun.requested_by == DEMO_MARKER,
            )
        )
    )
    for run in demo_runs:
        db.delete(run)

    db.flush()
    return {
        "imports": len(demo_imports),
        "deals": len(DEMO_DEAL_IDS),
        "contacts": len(DEMO_CONTACT_IDS),
        "companies": len(DEMO_COMPANY_IDS),
    }


def _ensure_stage_dictionary(db, portal_id: str) -> None:
    dictionary = db.scalar(
        select(CrmDictionary).where(
            CrmDictionary.portal_id == portal_id,
            CrmDictionary.entity_type_id == ENTITY_DEAL,
            CrmDictionary.dictionary_code == "status_DEAL_STAGE_15",
        )
    )
    if dictionary is None:
        dictionary = CrmDictionary(
            portal_id=portal_id,
            entity_type_id=ENTITY_DEAL,
            dictionary_code="status_DEAL_STAGE_15",
            title=f"{DEMO_PREFIX} — стадии",
            source_type="crm.status",
            source_hash=DEMO_MARKER,
            is_active=True,
        )
        db.add(dictionary)
        db.flush()

    existing = set(
        db.scalars(
            select(CrmDictionaryEntry.external_id).where(
                CrmDictionaryEntry.dictionary_id == dictionary.id
            )
        )
    )
    for index, (stage_id, title) in enumerate(STAGES):
        if stage_id in existing:
            continue
        db.add(
            CrmDictionaryEntry(
                dictionary_id=dictionary.id,
                external_id=stage_id,
                raw_value=title,
                normalized_value=title,
                sort_order=index * 10,
                is_active=True,
                source_hash=DEMO_MARKER,
            )
        )


def _seed_crm(db, portal_id: str) -> None:
    now = datetime.now(timezone.utc)
    company_with_deal = {
        "ID": DEMO_COMPANY_IDS[0],
        "TITLE": f"{DEMO_PREFIX} — ООО Проектный контур",
        "PHONE": [{"VALUE": "+7 900 200-00-01", "VALUE_TYPE": "WORK"}],
        "COMMENTS": "Синтетическая компания с привязанной сделкой и рабочим телефоном.",
    }
    company_without_deal = {
        "ID": DEMO_COMPANY_IDS[1],
        "TITLE": f"{DEMO_PREFIX} — ООО Без сделок",
        "PHONE": [{"VALUE": "+7 900 200-00-02", "VALUE_TYPE": "WORK"}],
        "COMMENTS": "Контрольный пример компании без единой сделки.",
    }
    for company_id, payload in zip(DEMO_COMPANY_IDS, (company_with_deal, company_without_deal)):
        db.add(
            CrmEntity(
                portal_id=portal_id,
                entity_type_id=ENTITY_COMPANY,
                entity_id=company_id,
                entity_kind="company",
                title=payload["TITLE"],
                raw_payload=payload,
                payload_hash=_payload_hash(payload),
                created_time=now - timedelta(days=120),
                updated_time=now,
                is_deleted=False,
            )
        )

    contact_repo = ContactRepository(db, portal_id)
    contact_cursor = 0
    for index, deal_id in enumerate(DEMO_DEAL_IDS):
        stage_id, stage_name = STAGES[index % len(STAGES)]
        region_id = REGIONS[index % len(REGIONS)]
        is_closed = stage_name == "Архив" and index % 2 == 0
        company_id = DEMO_COMPANY_IDS[0] if index in (2, 8, 20) else None
        payload = {
            "id": deal_id,
            "ID": deal_id,
            "TITLE": f"{DEMO_PREFIX} — сделка {index + 1:02d}",
            "CATEGORY_ID": 15,
            "STAGE_ID": stage_id,
            "UF_CRM_5ECE25C5D78E0": str(region_id),
            "ASSIGNED_BY_ID": 1 + index % 3,
            "closed": "Y" if is_closed else "N",
        }
        if company_id is not None:
            payload["COMPANY_ID"] = company_id
        db.add(
            CrmEntity(
                portal_id=portal_id,
                entity_type_id=ENTITY_DEAL,
                entity_id=deal_id,
                entity_kind="deal",
                title=payload["TITLE"],
                category_id=15,
                stage_id=stage_id,
                assigned_by_id=payload["ASSIGNED_BY_ID"],
                created_time=now - timedelta(days=index + 1),
                updated_time=now - timedelta(hours=index),
                closed_at=(now - timedelta(days=1)) if is_closed else None,
                raw_payload=payload,
                payload_hash=_payload_hash(payload),
                is_deleted=False,
            )
        )
        db.flush()

        # One deal intentionally has only a company phone.
        if index == 2:
            continue

        contact_id = DEMO_CONTACT_IDS[contact_cursor]
        contact_cursor += 1
        is_architect = index % 3 == 0
        position = "Главный архитектор" if is_architect else "Руководитель проекта"
        phone = f"+7 900 100-{index // 100:02d}-{index % 100:02d}"
        contact_repo.upsert_contact(
            contact_id,
            {
                "full_name": f"Тестовый контакт {index + 1:02d}",
                "post": position,
                "company_id": company_id,
                "company_title": company_with_deal["TITLE"] if company_id else None,
                "primary_phone": phone,
                "primary_phone_type": "MOBILE",
            },
            is_synthetic=True,
            raw_payload={
                "id": contact_id,
                "POST": position,
                "COMMENTS": f"{DEMO_PREFIX}: синтетический контакт",
                "DATE_CREATE": (now - timedelta(days=400 - index)).isoformat(),
            },
        )
        contact_repo.sync_phones(
            contact_id,
            [{"value": phone, "value_type": "MOBILE"}],
            phone,
        )
        contact_repo.upsert_link(contact_id, ENTITY_DEAL, deal_id, is_primary=True)

        # First deal has two contacts so that checkbox separation is visible.
        if index == 0:
            second_id = DEMO_CONTACT_IDS[contact_cursor]
            contact_cursor += 1
            second_phone = "+7 900 199-99-99"
            contact_repo.upsert_contact(
                second_id,
                {
                    "full_name": "Тестовый контакт — бухгалтер",
                    "post": "Главный бухгалтер",
                    "primary_phone": second_phone,
                    "primary_phone_type": "WORK",
                },
                is_synthetic=True,
                raw_payload={"id": second_id, "POST": "Главный бухгалтер"},
            )
            contact_repo.sync_phones(
                second_id,
                [{"value": second_phone, "value_type": "WORK"}],
                second_phone,
            )
            contact_repo.upsert_link(second_id, ENTITY_DEAL, deal_id, is_primary=False)


def _signals(**values) -> dict:
    base = {
        "positive": False,
        "alternate_contact_requested": False,
        "callback_later_requested": False,
        "no_answer": False,
        "deal_not_found": False,
        "explicit_refusal": False,
        "hangup_without_result": False,
        "hangup_during_robocall": False,
        "replacement_contact_required": False,
        "alternate_contact": {
            "name": None,
            "phone": None,
            "extension": None,
            "email": None,
            "position": None,
        },
        "callback_at": None,
        "callback_text": None,
        "summary": "",
        "refusal_reason": None,
        "confidence": 0.95,
        "needs_manual_review": False,
        "manual_review_reason": None,
    }
    base.update(values)
    return base


def _add_action(db, import_record, row, *, method: str, operation: str, summary: str, order: int) -> None:
    db.add(
        BitrixPreparedAction(
            import_id=import_record.id,
            import_row_id=row.id,
            action_group_id=f"demo-{row.source_row_number:02d}",
            method=method,
            action_type=operation,
            operation_type=operation,
            payload={"demo": True, "deal_id": row.matched_deal_id},
            human_summary=summary,
            validation_status="valid",
            is_enabled=True,
            idempotency_key=f"{DEMO_MARKER}:{row.id}:{operation}",
            execution_status="prepared",
            sort_order=order,
        )
    )


def _seed_call_results(db, portal_id: str) -> int:
    now = datetime.now(timezone.utc)
    import_record = CallResultImport(
        portal_id=portal_id,
        original_filename="demo-codex-all-groups.csv",
        campaign_name=f"{DEMO_PREFIX} — семь групп",
        storage_key="demo/demo-codex-all-groups.csv",
        file_sha256=hashlib.sha256(DEMO_MARKER.encode()).hexdigest(),
        file_size=0,
        status="ready",
        total_rows=7,
        matched_rows=7,
        review_rows=2,
        skipped_rows=0,
        created_by=DEMO_MARKER,
        processed_at=now,
        source_format="demo",
        batch_id="demo-codex-all-groups",
        deterministic_classified=7,
    )
    db.add(import_record)
    db.flush()

    callback_tomorrow = now + timedelta(days=1)
    callback_refusal = now + timedelta(days=92)
    definitions = (
        (
            "РАЗГОВОР БЫЛ, ДА",
            "hot_lead",
            "positive",
            _signals(positive=True, summary="Клиент подтвердил интерес и запросил КП"),
            False,
            None,
        ),
        (
            "РАЗГОВОР БЫЛ, НЕТ",
            "refusal",
            "refusal",
            _signals(
                explicit_refusal=True,
                summary="Клиент отказался",
                refusal_reason="Нет потребности",
                callback_at=callback_refusal.isoformat(),
            ),
            False,
            callback_refusal,
        ),
        (
            "ПЕРЕЗВОНИТЬ СЮДА",
            "manager_callback",
            "callback_later",
            _signals(
                callback_later_requested=True,
                callback_text="завтра в 10:00",
                callback_at=callback_tomorrow.isoformat(),
                summary="Попросили перезвонить завтра",
            ),
            False,
            callback_tomorrow,
        ),
        (
            "ПЕРЕЗВОНИТЬ ДРУГОМУ",
            "manager_callback",
            "alternate_contact",
            _signals(
                alternate_contact_requested=True,
                replacement_contact_required=True,
                alternate_contact={
                    "name": "Мария Ивановна",
                    "phone": "+7 900 300-00-04",
                    "extension": "123",
                    "email": None,
                    "position": "Архитектор",
                },
                summary="Передали контакт другого ответственного",
            ),
            False,
            None,
        ),
        (
            "РАЗГОВОР БЫЛ, НЕЯСНО",
            "unknown",
            "manual_review",
            _signals(summary="Содержательный разговор без однозначного результата"),
            True,
            None,
        ),
        (
            "НЕДОЗВОН",
            "robot_callback",
            "no_answer",
            _signals(no_answer=True, summary="Три попытки без ответа"),
            False,
            callback_tomorrow,
        ),
        (
            "ИНОЕ",
            "unknown",
            "manual_review",
            _signals(),
            True,
            None,
        ),
    )

    seeded_rows: list[CallResultImportRow] = []
    for index, (label, category, outcome, signals, manual_review, callback_at) in enumerate(definitions):
        deal_id = DEMO_DEAL_IDS[index]
        contact_id = db.scalar(
            select(CrmContactLink.contact_id).where(
                CrmContactLink.portal_id == portal_id,
                CrmContactLink.parent_entity_type_id == ENTITY_DEAL,
                CrmContactLink.parent_entity_id == deal_id,
                CrmContactLink.is_primary.is_(True),
            )
        )
        phone = f"7900100{index:04d}"
        row = CallResultImportRow(
            import_id=import_record.id,
            source_row_number=index + 2,
            raw_data={"demo": True, "expected_group": label},
            normalized_data={
                "has_meaningful_content": outcome not in ("no_answer", "manual_review") or bool(signals.get("summary")),
                "transcript": signals.get("summary") or label,
            },
            raw_phone=phone,
            normalized_phone=phone,
            category=category,
            comment=signals.get("summary") or label,
            call_id=f"demo-call-{index + 1}",
            campaign_id="demo-codex-all-groups",
            called_at=now - timedelta(minutes=10 * index),
            callback_at=callback_at,
            matched_contact_id=contact_id,
            matched_deal_id=deal_id,
            match_status="matched",
            match_reason="Синтетическое точное совпадение",
            technical_status="Completed" if outcome != "no_answer" else "No Answer",
            call_result_display="Completed" if outcome != "no_answer" else "No Answer",
            attempts=3 if outcome == "no_answer" else 1,
            llm_required=False,
            llm_status="not_required",
            deterministic_category=category,
            deterministic_reason="Синтетический сценарий",
            final_category=category,
            classification_source="deterministic",
            classification_reason="Синтетический сценарий для ручной проверки",
            business_signals=signals,
            primary_outcome=outcome,
            needs_manual_review=manual_review,
            manual_review_reason="Требуется решение менеджера" if manual_review else None,
            row_classifier_version="demo",
            row_planner_version="demo",
            execution_status="blocked_manual_review" if manual_review else "prepared",
        )
        db.add(row)
        db.flush()
        seeded_rows.append(row)

    _add_action(
        db,
        import_record,
        seeded_rows[0],
        method="tasks.task.add",
        operation="bitrix_add_task",
        summary="Создать задачу менеджеру",
        order=0,
    )
    _add_action(
        db,
        import_record,
        seeded_rows[1],
        method="crm.timeline.comment.add",
        operation="bitrix_add_comment",
        summary="Зафиксировать отказ",
        order=0,
    )

    retry_specs = (
        (seeded_rows[1], "refusal_followup", callback_refusal),
        (seeded_rows[2], "callback_later", callback_tomorrow),
        (seeded_rows[3], "alternate_contact", callback_tomorrow),
        (seeded_rows[5], "no_answer", callback_tomorrow),
    )
    for row, reason, callback_at in retry_specs:
        db.add(
            CallRetryQueueEntry(
                portal_id=portal_id,
                import_id=import_record.id,
                row_id=row.id,
                campaign_id=import_record.batch_id,
                source_call_id=row.call_id,
                deal_id=row.matched_deal_id,
                contact_id=row.matched_contact_id,
                phone_normalized=row.normalized_phone,
                callback_at=callback_at,
                callback_text=(row.business_signals or {}).get("callback_text"),
                reason=reason,
                status="pending",
                attempt_count=0,
                idempotency_key=f"{DEMO_MARKER}:retry:{row.id}:{reason}",
                timezone="Europe/Moscow",
            )
        )

    db.add(
        CallContactSearchEntry(
            portal_id=portal_id,
            import_id=import_record.id,
            row_id=seeded_rows[3].id,
            deal_id=seeded_rows[3].matched_deal_id,
            source_phone=seeded_rows[3].normalized_phone,
            source_contact_id=seeded_rows[3].matched_contact_id,
            deal_contact_ids=[seeded_rows[3].matched_contact_id],
            summary="Найти Марию Ивановну, архитектора, добавочный 123",
            call_id=seeded_rows[3].call_id,
            campaign_id=import_record.batch_id,
            status="contact_search_required",
        )
    )
    return import_record.id


def _seed_sync_state(db, portal_id: str) -> None:
    now = datetime.now(timezone.utc)
    db.add(
        SyncCheckpoint(
            portal_id=portal_id,
            resource_name="demo_codex",
            entity_type_id=ENTITY_DEAL,
            cursor_time=now,
            cursor_id=max(DEMO_DEAL_IDS),
            last_successful_sync_at=now,
            metadata_json={"demo": True, "marker": DEMO_MARKER},
        )
    )
    db.add(
        SyncRun(
            portal_id=portal_id,
            mode="full",
            status="completed",
            started_at=now - timedelta(seconds=5),
            finished_at=now,
            requested_by=DEMO_MARKER,
            current_phase="demo_ready",
            processed_count=len(DEMO_DEAL_IDS) + len(DEMO_CONTACT_IDS) + len(DEMO_COMPANY_IDS),
            created_count=len(DEMO_DEAL_IDS) + len(DEMO_CONTACT_IDS) + len(DEMO_COMPANY_IDS),
            statistics_json={"demo": True, "marker": DEMO_MARKER},
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", action="store_true", help="remove only DEMO-CODEX records")
    args = parser.parse_args()

    settings = get_settings()
    portal_id = resolve_portal_id(settings)
    with SessionLocal() as db:
        removed = _cleanup(db, portal_id)
        if args.clean:
            db.commit()
            print(f"Removed DEMO-CODEX data from portal {portal_id}: {removed}")
            return 0

        _ensure_stage_dictionary(db, portal_id)
        _seed_crm(db, portal_id)
        import_id = _seed_call_results(db, portal_id)
        _seed_sync_state(db, portal_id)
        db.commit()

    print(f"Seeded DEMO-CODEX data in portal {portal_id}")
    print(f"Deals: {len(DEMO_DEAL_IDS)}; contacts: {len(DEMO_CONTACT_IDS)}; companies: {len(DEMO_COMPANY_IDS)}")
    print(f"Call-result import: {import_id} (7 business groups)")
    print("No external services were called.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
