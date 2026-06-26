"""Preview parity: count reflects the capped selection and preview applies the
same transforms/validation/error routing as the export runner."""

from __future__ import annotations

from app.config import get_settings
from app.models import CrmEntity, ENTITY_DEAL
from app.services.export_plan.catalog import FieldCatalog
from app.services.export_plan.models_v2 import ExportPlan2
from app.services.export_plan.validator import ExportScope
from app.services.intelligent_export.dictionaries import build_dictionary_tools
from app.services.intelligent_export.preview_service import PreviewService
from app.services.intelligent_export.sheet_processor import make_sheet_processor
from app.services.intelligent_export.transform_engine import TransformContext

PORTAL = "test.bitrix24.ru"


def _seed_deal(db, eid, phone=None):
    payload = {"id": eid, "TITLE": f"Deal {eid}"}
    if phone is not None:
        payload["PHONE"] = [{"VALUE": phone, "VALUE_TYPE": "WORK"}]
    db.add(
        CrmEntity(
            portal_id=PORTAL,
            entity_type_id=ENTITY_DEAL,
            entity_id=eid,
            title=f"Deal {eid}",
            payload_hash=f"h{eid}",
            raw_payload=payload,
        )
    )


def _plan(*, limit=100, require_phone=False):
    phone_col = {
        "id": "phone",
        "header": "Телефон",
        "value": {"kind": "field", "field": {"entity_type_id": 2, "field_code": "PHONE", "source_alias": "deal"}},
        "transforms": [{"op": "phone_digits_only", "params": {}}],
    }
    sheet = {
        "id": "deals",
        "name": "Сделки",
        "mode": "rows",
        "dataset_id": "deals",
        "columns": [phone_col],
        "validation_rules": (
            [{"id": "ph", "type": "required", "column_id": "phone", "severity": "error"}] if require_phone else []
        ),
        "error_policy": "route_to_errors",
    }
    return ExportPlan2.model_validate(
        {
            "schema_version": "2.0",
            "title": "Экспорт",
            "datasets": [
                {"id": "deals", "primary_entity_type_id": 2, "sources": [{"alias": "deal", "entity_type_id": 2}], "limit": limit}
            ],
            "workbook": {"format": "xlsx", "filename_label": "deals", "sheets": [sheet]},
        }
    )


def _preview(db, *, with_processor=False):
    catalog = FieldCatalog.load(db, PORTAL)
    scope = ExportScope(role="admin", allow_sensitive_fields=True)
    processor = None
    if with_processor:
        resolve_label, dict_check = build_dictionary_tools(db, PORTAL)
        processor = make_sheet_processor(TransformContext(resolve_dictionary=resolve_label), dict_check)
    return PreviewService(db, get_settings(), PORTAL, scope, catalog, sheet_processor=processor)


def test_count_is_capped_by_dataset_limit(db_session):
    for i in range(1, 4):
        _seed_deal(db_session, i, phone="89991112233")
    db_session.commit()

    counts = _preview(db_session).count_datasets(_plan(limit=2))
    assert counts == {"deals": 2}  # 3 rows in DB, but selection limit is 2


def test_count_ignores_datasets_not_used_by_sheets(db_session):
    _seed_deal(db_session, 1, phone="89991112233")
    db_session.commit()
    plan = _plan(limit=100)
    # add an extra dataset that no sheet references
    plan.datasets.append(plan.datasets[0].model_copy(update={"id": "unused"}))

    counts = _preview(db_session).count_datasets(plan)
    assert "unused" not in counts
    assert set(counts) == {"deals"}


def test_preview_applies_validation_and_auto_errors_tab(db_session):
    _seed_deal(db_session, 1, phone="89991112233")
    _seed_deal(db_session, 2, phone=None)  # required phone missing -> routed
    db_session.commit()

    result = _preview(db_session, with_processor=True).preview(_plan(require_phone=True))

    data_sheet = result["sheets"][0]
    assert data_sheet["name"] == "Сделки"
    assert len(data_sheet["rows"]) == 1  # invalid row removed (matches export)

    errors_sheet = next(s for s in result["sheets"] if s["name"] == "Ошибки")
    assert errors_sheet["mode"] == "errors"
    assert len(errors_sheet["rows"]) == 1


def test_preview_without_processor_skips_validation(db_session):
    _seed_deal(db_session, 1, phone="89991112233")
    _seed_deal(db_session, 2, phone=None)
    db_session.commit()

    result = _preview(db_session, with_processor=False).preview(_plan(require_phone=True))
    # raw preview keeps both rows and adds no errors tab
    assert len(result["sheets"][0]["rows"]) == 2
    assert all(s["name"] != "Ошибки" for s in result["sheets"])
