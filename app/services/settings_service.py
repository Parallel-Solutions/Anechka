"""Сервис настроек в SQLite."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import SETTING_KEYS
from app.models import AppSetting, utcnow


def load_settings_from_db(db: Session) -> dict[str, str]:
    rows = db.query(AppSetting).filter(AppSetting.key.in_(SETTING_KEYS)).all()
    return {row.key: row.value for row in rows}


def save_settings_to_db(db: Session, values: dict[str, str]) -> None:
    for key in SETTING_KEYS:
        if key not in values:
            continue
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        if row:
            row.value = values[key]
            row.updated_at = utcnow()
        else:
            db.add(AppSetting(key=key, value=values[key]))
    db.commit()
