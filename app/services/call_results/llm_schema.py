"""Pydantic models and JSON schema for LLM classification v3 (multi-signal)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

PROMPT_VERSION = "3"
SCHEMA_VERSION = "2"
CLASSIFIER_VERSION = "2"
PLANNER_VERSION = "2"

PrimaryOutcome = Literal[
    "positive",
    "alternate_contact",
    "callback_later",
    "no_answer",
    "refusal",
    "hangup",
    "mixed",
    "manual_review",
    "unsupported_outcome",
]

# Legacy category for backward compat
Category = Literal["hot_lead", "manager_callback", "robot_callback", "refusal", "unknown"]


class AlternateContactData(BaseModel):
    name: str | None = None
    phone: str | None = None
    extension: str | None = None
    email: str | None = None
    position: str | None = None


class EvidenceItem(BaseModel):
    source_field: str = Field(default="")
    field: str = Field(default="")
    text: str = Field(max_length=300)

    @property
    def effective_field(self) -> str:
        return self.source_field or self.field


class CallResultSignals(BaseModel):
    positive: bool = False
    alternate_contact_requested: bool = False
    callback_later_requested: bool = False
    no_answer: bool = False
    deal_not_found: bool = False
    explicit_refusal: bool = False
    hangup_without_result: bool = False
    replacement_contact_required: bool = False
    alternate_contact: AlternateContactData = Field(default_factory=AlternateContactData)
    callback_at: datetime | None = None
    callback_text: str | None = None
    summary: str = ""
    refusal_reason: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_manual_review: bool = False
    manual_review_reason: str | None = None
    signal_reasons: dict[str, str] = Field(default_factory=dict)
    evidence: list[EvidenceItem] = Field(default_factory=list, max_length=5)

    def active_signal_count(self) -> int:
        return sum(
            1
            for flag in (
                self.positive,
                self.alternate_contact_requested,
                self.callback_later_requested,
                self.no_answer,
                self.explicit_refusal,
                self.hangup_without_result,
            )
            if flag
        )

    def to_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        return data


class CallResultLLMResult(BaseModel):
    """LLM structured output v3 — signals-first."""

    positive: bool = False
    alternate_contact_requested: bool = False
    callback_later_requested: bool = False
    no_answer: bool = False
    explicit_refusal: bool = False
    hangup_without_result: bool = False
    replacement_contact_required: bool = False
    alternate_contact: AlternateContactData = Field(default_factory=AlternateContactData)
    callback_at: datetime | None = None
    callback_text: str | None = None
    summary: str = Field(default="", max_length=2000)
    refusal_reason: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    needs_manual_review: bool = False
    manual_review_reason: str | None = None
    signal_reasons: dict[str, str] = Field(default_factory=dict)
    evidence: list[EvidenceItem] = Field(default_factory=list, max_length=5)
    # backward compat display
    primary_outcome: PrimaryOutcome | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_signal_reasons(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        reasons = data.get("signal_reasons")
        if isinstance(reasons, dict):
            data["signal_reasons"] = {
                str(k): str(v)
                for k, v in reasons.items()
                if v not in (None, "")
            }
        return data

    def to_signals(self) -> CallResultSignals:
        return CallResultSignals(
            positive=self.positive,
            alternate_contact_requested=self.alternate_contact_requested,
            callback_later_requested=self.callback_later_requested,
            no_answer=self.no_answer,
            explicit_refusal=self.explicit_refusal,
            hangup_without_result=self.hangup_without_result,
            replacement_contact_required=self.replacement_contact_required,
            alternate_contact=self.alternate_contact,
            callback_at=self.callback_at,
            callback_text=self.callback_text,
            summary=self.summary,
            refusal_reason=self.refusal_reason,
            confidence=self.confidence,
            needs_manual_review=self.needs_manual_review,
            manual_review_reason=self.manual_review_reason,
            signal_reasons=self.signal_reasons,
            evidence=self.evidence,
        )


def compute_primary_outcome(signals: CallResultSignals) -> str:
    if signals.needs_manual_review:
        return "manual_review"
    active = []
    if signals.positive:
        active.append("positive")
    if signals.alternate_contact_requested:
        active.append("alternate_contact")
    if signals.callback_later_requested:
        active.append("callback_later")
    if signals.no_answer:
        active.append("no_answer")
    if signals.explicit_refusal:
        active.append("refusal")
    if signals.hangup_without_result:
        active.append("hangup")
    if len(active) > 1:
        return "mixed"
    if len(active) == 1:
        return active[0]
    return "manual_review"


def legacy_category_from_signals(signals: CallResultSignals) -> str:
    """Map signals to legacy final_category for exports."""
    po = compute_primary_outcome(signals)
    mapping = {
        "positive": "hot_lead",
        "alternate_contact": "manager_callback",
        "callback_later": "manager_callback",
        "no_answer": "manager_callback",
        "refusal": "refusal",
        "hangup": "robot_callback",
        "mixed": "unknown",
        "manual_review": "unknown",
        "unsupported_outcome": "unknown",
    }
    return mapping.get(po, "unknown")


CALL_RESULT_CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "positive": {"type": "boolean"},
        "alternate_contact_requested": {"type": "boolean"},
        "callback_later_requested": {"type": "boolean"},
        "no_answer": {"type": "boolean"},
        "explicit_refusal": {"type": "boolean"},
        "hangup_without_result": {"type": "boolean"},
        "replacement_contact_required": {"type": "boolean"},
        "alternate_contact": {
            "type": "object",
            "properties": {
                "name": {"type": ["string", "null"]},
                "phone": {"type": ["string", "null"]},
                "extension": {"type": ["string", "null"]},
                "email": {"type": ["string", "null"]},
                "position": {"type": ["string", "null"]},
            },
            "required": ["name", "phone", "extension", "email", "position"],
            "additionalProperties": False,
        },
        "callback_at": {"type": ["string", "null"]},
        "callback_text": {"type": ["string", "null"]},
        "summary": {"type": "string"},
        "refusal_reason": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "needs_manual_review": {"type": "boolean"},
        "manual_review_reason": {"type": ["string", "null"]},
        "signal_reasons": {
            "type": "object",
            "properties": {
                "positive": {"type": ["string", "null"]},
                "alternate_contact_requested": {"type": ["string", "null"]},
                "callback_later_requested": {"type": ["string", "null"]},
                "no_answer": {"type": ["string", "null"]},
                "explicit_refusal": {"type": ["string", "null"]},
                "hangup_without_result": {"type": ["string", "null"]},
                "replacement_contact_required": {"type": ["string", "null"]},
            },
            "required": [
                "positive",
                "alternate_contact_requested",
                "callback_later_requested",
                "no_answer",
                "explicit_refusal",
                "hangup_without_result",
                "replacement_contact_required",
            ],
            "additionalProperties": False,
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_field": {"type": "string"},
                    "field": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["source_field", "field", "text"],
                "additionalProperties": False,
            },
        },
        "primary_outcome": {
            "type": ["string", "null"],
            "enum": [
                "positive", "alternate_contact", "callback_later", "refusal",
                "hangup", "mixed", "manual_review", "unsupported_outcome", None,
            ],
        },
    },
    "required": [
        "positive", "alternate_contact_requested", "callback_later_requested",
        "no_answer", "explicit_refusal", "hangup_without_result",
        "replacement_contact_required", "alternate_contact",
        "callback_at", "callback_text", "summary", "refusal_reason",
        "confidence", "needs_manual_review", "manual_review_reason",
        "signal_reasons", "evidence", "primary_outcome",
    ],
    "additionalProperties": False,
}
