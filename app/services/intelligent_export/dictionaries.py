"""Lightweight dictionary tools for label resolution and in_dictionary checks.

Resolves enumeration/user codes to human labels using locally imported data
(crm_users, crm_dictionaries/entries). Unknown dictionaries are treated as
permissive for validation (return True) to avoid false negatives in MVP.
"""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CrmDictionary, CrmDictionaryEntry, CrmUser


class DictionaryTools:
    def __init__(self, db: Session, portal_id: str):
        self.db = db
        self.portal_id = portal_id
        self._users: dict[str, str] | None = None
        self._dict_cache: dict[str, dict[str, str]] = {}

    def _users_map(self) -> dict[str, str]:
        if self._users is None:
            rows = self.db.scalars(select(CrmUser).where(CrmUser.portal_id == self.portal_id))
            self._users = {str(u.external_id): (u.display_name or str(u.external_id)) for u in rows}
        return self._users

    def _dict_map(self, code: str) -> dict[str, str]:
        if code in self._dict_cache:
            return self._dict_cache[code]
        mapping: dict[str, str] = {}
        dictionary = self.db.scalar(
            select(CrmDictionary).where(
                CrmDictionary.portal_id == self.portal_id, CrmDictionary.dictionary_code == code
            )
        )
        if dictionary is not None:
            entries = self.db.scalars(
                select(CrmDictionaryEntry).where(CrmDictionaryEntry.dictionary_id == dictionary.id)
            )
            for e in entries:
                mapping[str(e.external_id)] = e.raw_value or str(e.external_id)
        self._dict_cache[code] = mapping
        return mapping

    def resolve_label(self, dictionary_code: str | None, value: Any) -> Any:
        if value is None or value == "":
            return value
        key = str(value)
        if dictionary_code in (None, "users"):
            label = self._users_map().get(key)
            if label is not None:
                return label
            if dictionary_code == "users":
                return value
        if dictionary_code:
            return self._dict_map(dictionary_code).get(key, value)
        return value

    def check(self, dictionary_code: str, value: Any) -> bool:
        mapping = self._dict_map(dictionary_code)
        if not mapping:
            return True  # unknown dictionary => do not fail validation
        return str(value) in mapping


def build_dictionary_tools(db: Session, portal_id: str) -> tuple[Callable, Callable]:
    tools = DictionaryTools(db, portal_id)
    return tools.resolve_label, tools.check
