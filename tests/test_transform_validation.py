"""Phase F: transform engine, validation engine, sheet processor."""

from __future__ import annotations

from app.services.export_plan.catalog import FieldCatalog
from app.services.export_plan.models_v2 import Sheet
from app.services.intelligent_export.sheet_processor import process_sheet
from app.services.intelligent_export.transform_engine import TransformContext, apply_transforms
from app.services.export_plan.models_v2 import TransformStep


def _t(op, **params):
    return TransformStep(op=op, params=params)


def test_transform_phone_and_text():
    ctx = TransformContext()
    val, err = apply_transforms("8 (916) 123-45-67", [_t("phone_digits_only")], ctx)
    assert err is None and val == "79161234567"
    val, err = apply_transforms("  hi  ", [_t("trim"), _t("uppercase")], ctx)
    assert val == "HI"


def test_transform_phone_invalid_reports_error():
    ctx = TransformContext()
    val, err = apply_transforms("abc", [_t("phone_digits_only")], ctx)
    assert err == "phone_invalid"


def test_transform_empty_phone_is_not_error():
    ctx = TransformContext()
    val, err = apply_transforms(None, [_t("phone_normalize")], ctx)
    assert err is None and val is None
    val, err = apply_transforms("", [_t("phone_digits_only")], ctx)
    assert err is None and val is None


def test_transform_date_format():
    ctx = TransformContext()
    val, err = apply_transforms("2026-01-31T10:00:00+03:00", [_t("date_format", format="%d.%m.%Y")], ctx)
    assert err is None and val == "31.01.2026"


def test_transform_mapping_lookup_policies():
    ctx = TransformContext()
    steps = [_t("mapping_lookup", mapping={"NEW": "Новый"}, on_unknown="default", default="—")]
    assert apply_transforms("NEW", steps, ctx)[0] == "Новый"
    assert apply_transforms("XXX", steps, ctx)[0] == "—"
    steps_err = [_t("mapping_lookup", mapping={"NEW": "Новый"}, on_unknown="error")]
    assert apply_transforms("XXX", steps_err, ctx)[1] == "mapping_unknown_value"


def test_transform_dictionary_label_with_resolver():
    ctx = TransformContext(resolve_dictionary=lambda code, value: {"439": "Иванов"}.get(str(value)))
    val, err = apply_transforms(439, [_t("dictionary_label", dictionary_code="users")], ctx)
    assert val == "Иванов"


def _sheet(error_policy="route_to_errors"):
    return Sheet.model_validate(
        {
            "id": "s",
            "name": "S",
            "mode": "rows",
            "dataset_id": "d",
            "columns": [
                {"id": "phone", "header": "Тел", "value": {"kind": "field", "field": {"entity_type_id": 2, "field_code": "PHONE", "source_alias": "deal"}}, "transforms": [{"op": "phone_digits_only", "params": {}}]},
                {"id": "name", "header": "Имя", "value": {"kind": "field", "field": {"entity_type_id": 2, "field_code": "TITLE", "source_alias": "deal"}}},
            ],
            "validation_rules": [
                {"id": "phone_req", "type": "required", "column_id": "phone", "severity": "error"}
            ],
            "error_policy": error_policy,
        }
    )


def test_process_sheet_routes_errors():
    catalog = None  # not used by process_sheet directly
    rows = [
        {"phone": "8 916 123 45 67", "name": "Хороший"},
        {"phone": "плохой", "name": "Плохой"},
    ]
    data_rows, summary, error_rows = process_sheet(
        _sheet("route_to_errors"), rows, catalog, transform_ctx=TransformContext()
    )
    assert len(data_rows) == 1
    assert data_rows[0]["phone"] == "79161234567"
    assert len(error_rows) == 1
    assert "_errors" in error_rows[0]
    # the bad row fails both the phone transform and the required rule
    assert summary["error_count"] >= 1


def test_process_sheet_valid_only_drops_errors():
    rows = [{"phone": "плохой", "name": "X"}, {"phone": "89161234567", "name": "Y"}]
    data_rows, summary, error_rows = process_sheet(
        _sheet("valid_only"), rows, None, transform_ctx=TransformContext()
    )
    assert len(data_rows) == 1
    assert error_rows == []
