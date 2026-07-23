"""Persist the Tomoru callback without exposing it through normal settings."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import AppSetting


@dataclass(frozen=True)
class TomoruIntegrationState:
    portal_id: str
    callback_url: str
    bot_id: str
    subscribed_at: str


class TomoruIntegrationStore:
    """Store one callback per Bitrix portal in the existing settings table."""

    def __init__(self, db: Session, portal_id: str):
        self.db = db
        self.portal_id = portal_id

    @property
    def key(self) -> str:
        digest = hashlib.sha256(self.portal_id.encode("utf-8")).hexdigest()[:16]
        return f"tomoru_integration_{digest}"

    def load(self) -> TomoruIntegrationState | None:
        row = self.db.get(AppSetting, self.key)
        if row is None or not row.value:
            return None
        try:
            data = json.loads(row.value)
            return TomoruIntegrationState(
                portal_id=self.portal_id,
                callback_url=str(data["callback_url"]),
                bot_id=str(data.get("bot_id") or ""),
                subscribed_at=str(data["subscribed_at"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def save_callback(self, callback_url: str, *, bot_id: str = "") -> TomoruIntegrationState:
        state = TomoruIntegrationState(
            portal_id=self.portal_id,
            callback_url=callback_url,
            bot_id=bot_id,
            subscribed_at=datetime.now(timezone.utc).isoformat(),
        )
        value = json.dumps(asdict(state), ensure_ascii=False, sort_keys=True)
        row = self.db.get(AppSetting, self.key)
        if row is None:
            row = AppSetting(key=self.key, value=value)
            self.db.add(row)
        else:
            row.value = value
        self.db.flush()
        return state
