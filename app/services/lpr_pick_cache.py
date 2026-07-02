"""Cache for LPR pick results per deal (app_settings JSON blob)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import AppSetting, utcnow
from app.services.intelligent_export.contact_phone_heuristic import ContactCandidate
from app.services.lpr_service import LprConfig

LPR_PICK_CACHE_PREFIX = "lpr_pick_cache"


@dataclass
class CachedLprPick:
    input_hash: str
    contact_id: int
    reason: str
    confidence: float


def _settings_key(portal_id: str) -> str:
    return f"{LPR_PICK_CACHE_PREFIX}:{portal_id}"


def _parse_cache(raw: str | None) -> dict[int, CachedLprPick]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[int, CachedLprPick] = {}
    for key, val in data.items():
        try:
            deal_id = int(key)
        except (TypeError, ValueError):
            continue
        if not isinstance(val, dict):
            continue
        try:
            contact_id = int(val.get("contact_id"))
            confidence = float(val.get("confidence", 0))
        except (TypeError, ValueError):
            continue
        input_hash = str(val.get("input_hash") or "")
        reason = str(val.get("reason") or "")
        if not input_hash:
            continue
        out[deal_id] = CachedLprPick(
            input_hash=input_hash,
            contact_id=contact_id,
            reason=reason,
            confidence=max(0.0, min(100.0, confidence)),
        )
    return out


def _serialize_cache(cache: dict[int, CachedLprPick]) -> str:
    payload = {
        str(deal_id): {
            "input_hash": pick.input_hash,
            "contact_id": pick.contact_id,
            "reason": pick.reason,
            "confidence": pick.confidence,
        }
        for deal_id, pick in cache.items()
    }
    return json.dumps(payload, ensure_ascii=False)


def compute_input_hash(
    deal_title: str,
    candidates: list[ContactCandidate],
    lpr_config: LprConfig,
) -> str:
    parts: list[str] = [deal_title or ""]
    for cand in sorted(candidates, key=lambda c: c.contact_id):
        contact = cand.contact
        parts.append(
            "|".join(
                [
                    str(cand.contact_id),
                    str(contact.post or ""),
                    str(contact.post_custom or ""),
                    str(contact.last_name or ""),
                ]
            )
        )
    keywords_blob = hashlib.sha256(
        json.dumps(lpr_config.keywords, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    parts.append(keywords_blob)
    blob = "\n".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_all(db: Session, portal_id: str) -> dict[int, CachedLprPick]:
    row = db.query(AppSetting).filter(AppSetting.key == _settings_key(portal_id)).first()
    return _parse_cache(row.value if row else None)


def save_all(db: Session, portal_id: str, cache: dict[int, CachedLprPick]) -> None:
    key = _settings_key(portal_id)
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    payload = _serialize_cache(cache)
    if row:
        row.value = payload
        row.updated_at = utcnow()
    else:
        db.add(AppSetting(key=key, value=payload))
    db.commit()


def get_cached(
    cache: dict[int, CachedLprPick],
    deal_id: int,
    input_hash: str,
) -> CachedLprPick | None:
    entry = cache.get(int(deal_id))
    if entry is None or entry.input_hash != input_hash:
        return None
    return entry


def set_cached(
    cache: dict[int, CachedLprPick],
    deal_id: int,
    *,
    input_hash: str,
    contact_id: int,
    reason: str,
    confidence: float,
) -> None:
    cache[int(deal_id)] = CachedLprPick(
        input_hash=input_hash,
        contact_id=int(contact_id),
        reason=reason,
        confidence=max(0.0, min(100.0, confidence)),
    )


def clear_all(db: Session) -> None:
    prefix = f"{LPR_PICK_CACHE_PREFIX}:"
    rows = db.query(AppSetting).filter(AppSetting.key.like(f"{prefix}%")).all()
    for row in rows:
        db.delete(row)
    if rows:
        db.commit()
