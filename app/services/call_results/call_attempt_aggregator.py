"""Aggregate call attempts by phone and detect exact duplicates."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

_CATEGORY_PRIORITY = {
    "refusal": 100,
    "hot_lead": 80,
    "manager_callback": 60,
    "unknown": 40,
    "robot_callback": 20,
}


@dataclass
class AttemptGroup:
    normalized_phone: str
    attempts: list[dict[str, Any]] = field(default_factory=list)
    latest_outcome: str | None = None
    latest_no_answer: bool = False


def scenario_events_hash(events: list[dict] | None) -> str:
    if not events:
        return ""
    canonical = json.dumps(events, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def exact_duplicate_key(
    *,
    source_format: str | None,
    batch_id: str | None,
    normalized_phone: str | None,
    last_attempt_at: datetime | None,
    technical_status: str | None,
    call_result_display: str | None,
    scenario_events: list[dict] | None,
    row_hash_fallback: str | None = None,
) -> str:
    if last_attempt_at:
        ts = last_attempt_at.isoformat()
    else:
        ts = row_hash_fallback or ""
    parts = [
        source_format or "",
        batch_id or "",
        normalized_phone or "",
        ts,
        (technical_status or "").lower(),
        (call_result_display or "").lower(),
        scenario_events_hash(scenario_events),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def build_source_identity(
    *,
    batch_id: str | None,
    phone: str | None,
    last_attempt_at: datetime | None,
    content_hash: str,
) -> str:
    ts = last_attempt_at.isoformat() if last_attempt_at else ""
    raw = f"{batch_id or ''}:{phone or ''}:{ts}:{content_hash}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class CallAttemptAggregator:
    """Group rows by phone and compute attempt history summaries."""

    def group_by_phone(self, rows: list[Any]) -> dict[str, AttemptGroup]:
        groups: dict[str, AttemptGroup] = {}
        for row in rows:
            phone = row.normalized_phone
            if not phone:
                continue
            g = groups.setdefault(phone, AttemptGroup(normalized_phone=phone))
            cat = row.final_category or row.deterministic_category
            g.attempts.append({
                "row_id": row.id,
                "source_row_number": row.source_row_number,
                "called_at": row.called_at.isoformat() if row.called_at else None,
                "technical_status": row.technical_status,
                "call_result_display": row.call_result_display,
                "final_category": cat,
                "has_content": (row.normalized_data or {}).get("has_meaningful_content"),
            })
        for g in groups.values():
            g.attempts.sort(key=lambda a: a.get("called_at") or "")
            self._compute_outcome(g)
        return groups

    @staticmethod
    def _compute_outcome(group: AttemptGroup) -> None:
        best_cat = None
        best_prio = -1
        latest_no_answer = False
        for att in group.attempts:
            cat = att.get("final_category")
            cr = (att.get("call_result_display") or "").lower()
            if cr in ("no answer", "busy", "voicemail", "not started"):
                latest_no_answer = True
            if cat:
                prio = _CATEGORY_PRIORITY.get(cat, 0)
                if prio > best_prio:
                    best_prio = prio
                    best_cat = cat
            elif att.get("has_content"):
                best_prio = max(best_prio, 50)
                best_cat = best_cat or "manager_callback"
        group.latest_outcome = best_cat
        group.latest_no_answer = latest_no_answer and best_prio < 60
