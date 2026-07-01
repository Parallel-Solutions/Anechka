"""Build minimal LLM input and compute input hash."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class LlmInputBundle:
    payload: dict[str, Any]
    input_hash: str
    truncated: bool
    substantial_loss: bool


class LlmInputBuilder:
    def __init__(self, max_chars: int = 12000):
        self.max_chars = max_chars

    def build(
        self,
        row: dict[str, Any],
        *,
        prompt_version: str,
        schema_version: str,
        model: str,
        crm_context: dict[str, Any] | None = None,
    ) -> LlmInputBundle:
        if row.get("source_format") == "tomoru_csv":
            payload = self._build_tomoru(row, crm_context)
        else:
            payload = {
                "source": "generic",
                "technical_result": row.get("technical_result") or row.get("category"),
                "call_summary": row.get("comment") or row.get("call_summary"),
                "transcript": row.get("transcript"),
                "scenario_answers": row.get("scenario_answers") if isinstance(row.get("scenario_answers"), dict) else {},
                "called_at": row.get("called_at"),
                "region": row.get("region"),
                "known_contact_name": row.get("contact_name"),
            }

        truncated = False
        substantial_loss = False
        text = self._combined_text(payload)
        if len(text) > self.max_chars:
            truncated = True
            head = text[:500]
            tail = text[-1500:]
            new_text = f"{head}\n...[обрезано]...\n{tail}"
            if payload.get("source") == "tomoru":
                payload["content_text"] = new_text
            elif payload.get("transcript"):
                payload["transcript"] = new_text
            else:
                payload["call_summary"] = new_text
            substantial_loss = len(text) > self.max_chars * 2

        hash_input = "|".join([
            self._normalize_for_hash(json.dumps(payload, sort_keys=True, default=str)),
            prompt_version,
            schema_version,
            model,
        ])
        input_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()
        return LlmInputBundle(
            payload=payload,
            input_hash=input_hash,
            truncated=truncated,
            substantial_loss=substantial_loss,
        )

    @staticmethod
    def _build_tomoru(row: dict[str, Any], crm_context: dict[str, Any] | None) -> dict[str, Any]:
        events = row.get("scenario_events") or []
        slim_events = [
            {
                "field": e.get("field"),
                "match": e.get("match"),
                "transcription": e.get("transcription"),
                "source_column": e.get("source_column"),
            }
            for e in events
            if isinstance(e, dict)
        ]
        return {
            "source": "tomoru",
            "technical": {
                "status": row.get("status"),
                "call_result": row.get("call_result") or row.get("technical_result"),
                "attempts": row.get("attempts"),
                "last_attempt_at": row.get("called_at"),
            },
            "scenario_events": slim_events,
            "content_text": row.get("content_text") or "",
            "known_crm_context": crm_context or {},
        }

    @staticmethod
    def _combined_text(payload: dict[str, Any]) -> str:
        if payload.get("source") == "tomoru":
            return str(payload.get("content_text") or "")
        parts = [
            str(payload.get("call_summary") or ""),
            str(payload.get("transcript") or ""),
        ]
        return "\n".join(p for p in parts if p)

    @staticmethod
    def _normalize_for_hash(val: Any) -> str:
        if val is None:
            return ""
        return re.sub(r"\s+", " ", str(val).strip().lower())

    @staticmethod
    def source_text(row: dict[str, Any]) -> str:
        parts: list[str] = []
        if row.get("content_text"):
            parts.append(str(row["content_text"]))
        for ev in row.get("scenario_events") or []:
            if isinstance(ev, dict):
                for k in ("match", "transcription"):
                    if ev.get(k):
                        parts.append(str(ev[k]))
        for k in ("comment", "transcript", "call_summary", "technical_result", "category"):
            v = row.get(k)
            if v:
                parts.append(str(v))
        return " ".join(parts).lower()

    @staticmethod
    def source_columns(row: dict[str, Any]) -> set[str]:
        cols = set()
        for ev in row.get("scenario_events") or []:
            if isinstance(ev, dict) and ev.get("source_column"):
                cols.add(str(ev["source_column"]))
        return cols
