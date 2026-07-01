"""Tomoru CSV call result adapter."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

TOMORU_REQUIRED_COLUMNS = frozenset({
    "phone_number",
    "status_display",
    "call_result_display",
    "attempts",
    "last_attempt_at",
})

DATA_COLUMN_PREFIX = "data:"

ADAPTER_VERSION = "1"


@dataclass
class TomoruRowResult:
    normalized: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


class TomoruCallResultAdapter:
    """Parse Tomoru export rows into normalized call-result structure."""

    @classmethod
    def is_tomoru_format(cls, headers: list[str]) -> bool:
        norm = {cls._norm_header(h) for h in headers if h}
        if not TOMORU_REQUIRED_COLUMNS.issubset(norm):
            return False
        return any(cls._norm_header(h).startswith(DATA_COLUMN_PREFIX) for h in headers if h)

    @classmethod
    def auto_mapping(cls, headers: list[str]) -> dict[str, str]:
        """Fixed Tomoru column mapping."""
        mapping: dict[str, str] = {}
        for h in headers:
            nh = cls._norm_header(h)
            if nh == "phone_number":
                mapping["phone"] = h
            elif nh == "status_display":
                mapping["status"] = h
            elif nh == "call_result_display":
                mapping["technical_result"] = h
            elif nh == "attempts":
                mapping["attempts"] = h
            elif nh == "last_attempt_at":
                mapping["called_at"] = h
        return mapping

    @classmethod
    def data_columns(cls, headers: list[str]) -> list[tuple[str, str]]:
        """Return (source_column, field_name) for each data:* column."""
        out: list[tuple[str, str]] = []
        for h in headers:
            nh = cls._norm_header(h)
            if nh.startswith(DATA_COLUMN_PREFIX):
                field_name = h.split(":", 1)[1] if ":" in h else nh[len(DATA_COLUMN_PREFIX):]
                out.append((h, field_name.strip()))
        return out

    def normalize_row(
        self,
        raw_row: dict[str, Any],
        headers: list[str],
        *,
        batch_id: str | None = None,
        exported_at: str | None = None,
    ) -> TomoruRowResult:
        warnings: list[str] = []
        mapping = self.auto_mapping(headers)
        flat: dict[str, Any] = {}
        for field_name, header in mapping.items():
            if header in raw_row:
                val = raw_row[header]
                if val is not None and str(val).strip() != "":
                    flat[field_name] = val

        scenario_events: list[dict[str, Any]] = []
        for source_col, field_name in self.data_columns(headers):
            cell = raw_row.get(source_col)
            if cell is None or str(cell).strip() == "":
                continue
            event, warn = self._parse_data_cell(source_col, field_name, str(cell))
            if warn:
                warnings.append(warn)
            if event:
                scenario_events.append(event)

        content_text = self._build_content_text(scenario_events)
        has_meaningful = bool(scenario_events) or bool(content_text.strip())

        technical_result = str(flat.get("technical_result") or "").strip()
        status = str(flat.get("status") or "").strip()

        normalized: dict[str, Any] = {
            "source_format": "tomoru_csv",
            "phone": str(flat.get("phone") or "").strip(),
            "status": status,
            "technical_result": technical_result,
            "category": technical_result,
            "call_result": technical_result,
            "attempts": self._parse_attempts(flat.get("attempts")),
            "called_at": flat.get("called_at"),
            "scenario_events": scenario_events,
            "scenario_answers": {},
            "content_text": content_text,
            "has_meaningful_content": has_meaningful,
            "batch_id": batch_id,
            "exported_at": exported_at,
        }
        if content_text:
            normalized["comment"] = content_text
        return TomoruRowResult(normalized=normalized, warnings=warnings)

    def _parse_data_cell(
        self,
        source_col: str,
        field_name: str,
        raw_value: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        text = raw_value.strip()
        if not text:
            return None, None

        parsed: dict[str, Any] | None = None
        warning: str | None = None
        try:
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                parsed = {"value": parsed}
        except json.JSONDecodeError:
            warning = f"Повреждённый JSON в {source_col}"
            parsed = {"raw_text": text}

        match = None
        transcription = None
        extra: dict[str, Any] = {}
        if isinstance(parsed, dict):
            match = parsed.get("match")
            transcription = parsed.get("transcription")
            for k, v in parsed.items():
                if k not in ("match", "transcription"):
                    extra[k] = v

        event: dict[str, Any] = {
            "field": field_name,
            "source_column": source_col,
            "match": str(match).strip() if match is not None else None,
            "transcription": str(transcription).strip() if transcription is not None else None,
            "raw": parsed if isinstance(parsed, dict) else {"raw_text": text},
        }
        if extra:
            event["extra"] = extra
        if warning and "raw_text" not in event["raw"]:
            event["raw"]["raw_text"] = text
        return event, warning

    @staticmethod
    def _build_content_text(events: list[dict[str, Any]]) -> str:
        seen_fragments: set[str] = set()
        blocks: list[str] = []
        for ev in events:
            field = ev.get("field") or "?"
            parts: list[str] = []
            match = ev.get("match")
            transcription = ev.get("transcription")
            if match:
                frag = f"match:{match}"
                if frag not in seen_fragments:
                    seen_fragments.add(frag)
                    parts.append(f"match: {match}")
            if transcription:
                frag = f"transcription:{transcription}"
                if frag not in seen_fragments:
                    seen_fragments.add(frag)
                    parts.append(f"transcription: {transcription}")
            if parts:
                blocks.append(f"[{field}]\n" + "\n".join(parts))
        return "\n\n".join(blocks)

    @staticmethod
    def _parse_attempts(val: Any) -> int | None:
        if val is None or str(val).strip() == "":
            return None
        try:
            return int(float(str(val).strip()))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _norm_header(h: str) -> str:
        return re.sub(r"[\s_\-]+", "_", h.strip().lower())
