"""Portal identification from Bitrix webhook URL."""

from __future__ import annotations

from urllib.parse import urlparse


def portal_id_from_webhook(webhook_url: str) -> str:
    if not webhook_url:
        return "default"
    parsed = urlparse(webhook_url.rstrip("/"))
    host = parsed.netloc or parsed.path.split("/")[0]
    return host or "default"


def bitrix_deal_url(portal_id: str, deal_id: int) -> str | None:
    if not portal_id or portal_id == "default":
        return None
    return f"https://{portal_id}/crm/deal/details/{deal_id}/"


def bitrix_contact_url(portal_id: str, contact_id: int) -> str | None:
    if not portal_id or portal_id == "default":
        return None
    return f"https://{portal_id}/crm/contact/details/{contact_id}/"


def bitrix_company_url(portal_id: str, company_id: int) -> str | None:
    if not portal_id or portal_id == "default":
        return None
    return f"https://{portal_id}/crm/company/details/{company_id}/"
