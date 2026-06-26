"""Portal identification from Bitrix webhook URL."""

from __future__ import annotations

from urllib.parse import urlparse


def portal_id_from_webhook(webhook_url: str) -> str:
    if not webhook_url:
        return "default"
    parsed = urlparse(webhook_url.rstrip("/"))
    host = parsed.netloc or parsed.path.split("/")[0]
    return host or "default"
