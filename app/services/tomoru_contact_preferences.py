"""Persisted Tomoru contact selection per deal (app_settings JSON blob)."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models import AppSetting, utcnow

TOMORU_CONTACT_OVERRIDES_PREFIX = "tomoru_contact_overrides"


def _settings_key(portal_id: str) -> str:
    return f"{TOMORU_CONTACT_OVERRIDES_PREFIX}:{portal_id}"


def _parse_overrides(raw: str | None) -> dict[int, list[int]]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[int, list[int]] = {}
    for key, val in data.items():
        try:
            deal_id = int(key)
        except (TypeError, ValueError):
            continue
        ids: list[int] = []
        if isinstance(val, list):
            for item in val:
                try:
                    ids.append(int(item))
                except (TypeError, ValueError):
                    continue
        else:
            try:
                ids.append(int(val))
            except (TypeError, ValueError):
                continue
        out[deal_id] = list(dict.fromkeys(ids))
    return out


def load_all(db: Session, portal_id: str) -> dict[int, list[int]]:
    row = db.query(AppSetting).filter(AppSetting.key == _settings_key(portal_id)).first()
    return _parse_overrides(row.value if row else None)


def set_deal(db: Session, portal_id: str, deal_id: int, contact_ids: list[int]) -> None:
    key = _settings_key(portal_id)
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    overrides = _parse_overrides(row.value if row else None)
    normalized = list(dict.fromkeys(int(cid) for cid in contact_ids))
    overrides[int(deal_id)] = normalized
    payload = json.dumps(
        {str(k): v for k, v in overrides.items()},
        ensure_ascii=False,
    )
    if row:
        row.value = payload
        row.updated_at = utcnow()
    else:
        db.add(AppSetting(key=key, value=payload))
    db.commit()


def merge_with_request(
    saved: dict[int, list[int]],
    request_overrides: dict[int, list[int]],
) -> dict[int, list[int]]:
    """Request overrides win over saved preferences for the same deal."""
    return {**saved, **request_overrides}
