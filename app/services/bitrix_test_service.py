"""Manual Bitrix write tests from the settings page."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Settings
from app.exceptions import BitrixAPIError
from app.schemas import BitrixTestResponse
from app.services.bitrix_client import BitrixClient
from app.services.call_results.bitrix_gateway import CallResultsBitrixGateway, build_bitrix_gateway


def _load_deal(client: BitrixClient, deal_id: int) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = client.call("crm.deal.get", {"id": deal_id})
    except BitrixAPIError as exc:
        return None, str(exc)
    result = data.get("result")
    if not result or not result.get("ID"):
        return None, f"Сделка {deal_id} не найдена"
    return result, None


def _gateway(settings: Settings, gateway: CallResultsBitrixGateway | None = None) -> tuple[BitrixClient, CallResultsBitrixGateway]:
    client = BitrixClient(settings)
    gw = gateway or build_bitrix_gateway(settings, client)
    return client, gw


def test_add_comment(settings: Settings, deal_id: int, *, gateway: CallResultsBitrixGateway | None = None) -> BitrixTestResponse:
    client, gw = _gateway(settings, gateway)
    deal, err = _load_deal(client, deal_id)
    if err:
        return BitrixTestResponse(ok=False, message=err)

    payload = {
        "fields": {
            "ENTITY_ID": deal_id,
            "ENTITY_TYPE": "deal",
            "COMMENT": "[Тест] Проверка комментария Bitrix Export",
        }
    }
    res = gw.add_deal_comment(payload)
    if not res.success:
        return BitrixTestResponse(ok=False, message=res.error or "Не удалось добавить комментарий")
    msg = "Комментарий добавлен"
    if res.external_id:
        msg = f"{msg} (ID: {res.external_id})"
    return BitrixTestResponse(ok=True, message=msg, external_id=res.external_id)


def test_add_todo(settings: Settings, deal_id: int, *, gateway: CallResultsBitrixGateway | None = None) -> BitrixTestResponse:
    client, gw = _gateway(settings, gateway)
    deal, err = _load_deal(client, deal_id)
    if err:
        return BitrixTestResponse(ok=False, message=err)

    assigned_by_id = deal.get("ASSIGNED_BY_ID")
    deadline = datetime.now(timezone.utc) + timedelta(hours=24)
    payload: dict[str, Any] = {
        "ownerTypeId": 2,
        "ownerId": deal_id,
        "title": "[Тест] Дело по обзвону",
        "description": "Тестовое CRM-дело из настроек",
        "pingOffsets": [0, 15],
        "deadline": deadline.isoformat(),
    }
    if assigned_by_id:
        payload["responsibleId"] = int(assigned_by_id)

    res = gw.add_deal_todo(payload)
    if not res.success:
        return BitrixTestResponse(ok=False, message=res.error or "Не удалось создать дело")
    msg = "CRM-дело создано"
    if res.external_id:
        msg = f"{msg} (ID: {res.external_id})"
    return BitrixTestResponse(ok=True, message=msg, external_id=res.external_id)


def test_create_contact(settings: Settings, deal_id: int, *, gateway: CallResultsBitrixGateway | None = None) -> BitrixTestResponse:
    client, gw = _gateway(settings, gateway)
    _, err = _load_deal(client, deal_id)
    if err:
        return BitrixTestResponse(ok=False, message=err)

    ts = int(datetime.now(timezone.utc).timestamp())
    fields: dict[str, Any] = {
        "NAME": "Тест",
        "LAST_NAME": "Anechka",
        "PHONE": [{"VALUE": f"+7999{ts % 10000000:07d}", "VALUE_TYPE": "WORK"}],
        "COMMENTS": "[Тест] контакт из настроек",
    }
    code = getattr(settings, "bitrix_call_source_field_code", "") or ""
    val = getattr(settings, "bitrix_call_source_field_value", "") or ""
    if code and val:
        fields[code] = val

    res = gw.create_contact(fields)
    if not res.success or not res.external_id:
        return BitrixTestResponse(ok=False, message=res.error or "Не удалось создать контакт")

    contact_id = int(res.external_id)
    link = gw.link_contact_to_deal(deal_id, contact_id)
    if not link.success:
        return BitrixTestResponse(
            ok=False,
            message=f"Контакт создан (ID: {contact_id}), но не удалось привязать к сделке: {link.error}",
            external_id=res.external_id,
        )

    msg = f"Контакт создан и привязан к сделке (ID контакта: {contact_id})"
    return BitrixTestResponse(ok=True, message=msg, external_id=res.external_id)
