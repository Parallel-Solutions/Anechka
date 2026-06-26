"""Conversation orchestration for the intelligent export chat.

Builds the planner context from the catalog/registry/memory/history, runs a
planner turn, persists the user and assistant messages, and (when the candidate
validates) appends an immutable plan version recording applied memory.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings
from app.repositories.intelligent_export_repository import IntelligentExportRepository
from app.models import ENTITY_CONTACT, ENTITY_DEAL
from app.services.export_plan.catalog import DENORM_FIELD_MAP, FieldCatalog, FieldCatalogEntry
from app.services.export_plan.registry import registry_descriptor
from app.services.export_plan.validator import ExportScope
from app.services.intelligent_export.plan_service import (
    format_fix_suggestions,
    format_repair_failure_message,
    validation_to_dict,
)
from app.services.intelligent_export.errors import ie_error
from app.services.intelligent_export.planner import BasePlanner, PlannerResult, plan_turn
from app.services.intelligent_export.readiness import compute_readiness
from app.services.intelligent_export.response_formatter import build_plan_summary, format_assistant_message

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

CONTACT_CORE_FIELDS = ("NAME", "LAST_NAME", "SECOND_NAME", "POST", "COMMENTS", "PHONE", "EMAIL", "TITLE")
CONTACT_REQUEST_TOKENS = frozenset(
    {"контакт", "контакты", "телефон", "телефоны", "имя", "имена", "должност", "contact", "phone", "phones"}
)
CITY_REQUEST_TOKENS = frozenset({"город", "красноярск", "регион", "адрес", "city"})


def _no_data_error(portal_id: str):
    return ie_error(
        "NO_DATA",
        "В подключённой базе нет импортированных данных CRM для портала "
        f"«{portal_id}». Сначала выполните импорт — планировщик и выгрузка пока недоступны.",
    )


def _memory_content_hash(entry) -> str:
    payload = f"{entry.kind}:{entry.key}:{entry.content or ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _extract_field_codes(node: Any) -> set[str]:
    """Collect every field_code referenced anywhere in a plan dict."""
    codes: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "field_code" and isinstance(value, str):
                codes.add(value.upper())
            else:
                codes |= _extract_field_codes(value)
    elif isinstance(node, list):
        for item in node:
            codes |= _extract_field_codes(item)
    return codes


def _tokens(*texts: str) -> list[str]:
    seen: list[str] = []
    out: set[str] = set()
    for text in texts:
        for tok in _TOKEN_RE.findall(text or ""):
            low = tok.lower()
            if len(low) >= 3 and low not in out:
                out.add(low)
                seen.append(low)
    return seen


class IntelligentExportService:
    def __init__(self, db: Session, settings: Settings, portal_id: str):
        self.db = db
        self.settings = settings
        self.portal_id = portal_id

    def build_context(
        self,
        scope: ExportScope,
        repo: IntelligentExportRepository,
        conversation_id: int,
        current_plan_json: dict | None,
        message: str = "",
    ) -> tuple[dict[str, Any], list]:
        catalog = FieldCatalog.load(self.db, self.portal_id)
        memory = repo.list_memory(status="approved")
        history = repo.list_messages(conversation_id)[-self.settings.ie_max_history_messages :]
        context = {
            "today": date.today().isoformat(),
            "scope": {
                "role": scope.role,
                "allowed_entity_type_ids": sorted(scope.allowed_entity_type_ids)
                if scope.allowed_entity_type_ids
                else "all",
                "assigned_by_id": scope.assigned_by_id,
                "max_rows": scope.max_rows,
                "allow_sensitive_fields": scope.allow_sensitive_fields,
            },
            "catalog": self._select_catalog(catalog, scope, message, current_plan_json, memory),
            "registry": registry_descriptor(),
            "memory": [
                {"id": m.id, "scope": m.scope, "kind": m.kind, "key": m.key, "content": m.content}
                for m in memory
            ],
            "history": [{"role": m.role, "content": m.content} for m in history],
            "current_plan": current_plan_json,
        }
        return context, memory

    def _select_catalog(
        self,
        catalog: FieldCatalog,
        scope: ExportScope,
        message: str,
        current_plan_json: dict | None,
        memory: list,
    ) -> list[dict]:
        """Relevance-budgeted catalog descriptor.

        Returns the full catalog when it fits the budget; otherwise keeps core
        denormalized fields, every field referenced by the current plan, and the
        fields most relevant to the request (and memory aliases/terms), capped by
        ``ie_catalog_field_budget``. This avoids ever truncating the JSON payload
        mid-structure, which previously corrupted the catalog and hurt quality.
        """
        include_sensitive = scope.allow_sensitive_fields
        budget = max(1, self.settings.ie_catalog_field_budget)

        def visible(key: tuple[int, str], entry: FieldCatalogEntry) -> bool:
            if key in catalog.denied_fields:
                return False
            if entry.sensitive and not include_sensitive:
                return False
            return True

        all_entries = [
            (key, entry) for key, entry in sorted(catalog.fields.items()) if visible(key, entry)
        ]
        if len(all_entries) <= budget:
            return [entry.descriptor() for _, entry in all_entries]

        selected: dict[tuple[int, str], FieldCatalogEntry] = {}

        # 1) core denormalized fields — always useful as defaults
        for key, entry in all_entries:
            if entry.field_code in DENORM_FIELD_MAP:
                selected[key] = entry

        # 2) fields referenced by the current plan (so edits keep them in scope)
        plan_codes = _extract_field_codes(current_plan_json)
        if plan_codes:
            for key, entry in all_entries:
                if entry.field_code in plan_codes:
                    selected[key] = entry

        message_tokens = set(_tokens(message))
        if message_tokens & CONTACT_REQUEST_TOKENS:
            for code in CONTACT_CORE_FIELDS:
                key = (ENTITY_CONTACT, code)
                if key in catalog.fields and visible(key, catalog.fields[key]):
                    selected[key] = catalog.fields[key]

        if message_tokens & CITY_REQUEST_TOKENS:
            for query in ("город", "адрес", "регион"):
                for entry in catalog.search(query, entity_type_id=ENTITY_DEAL, limit=5):
                    key = (entry.entity_type_id, entry.field_code)
                    if visible(key, entry):
                        selected[key] = entry

        # 3) relevance to the request and memory aliases/terms
        memory_text = " ".join(
            f"{m.key or ''} {m.content or ''}"
            for m in memory
            if getattr(m, "kind", None) in ("alias", "term", "mapping")
        )
        for token in _tokens(message, memory_text):
            if len(selected) >= budget:
                break
            for entry in catalog.search(token, limit=10):
                key = (entry.entity_type_id, entry.field_code)
                if not visible(key, entry):
                    continue
                selected.setdefault(key, entry)
                if len(selected) >= budget:
                    break

        ordered = list(selected.items())[:budget]
        return [entry.descriptor() for _, entry in ordered]

    def chat(
        self,
        repo: IntelligentExportRepository,
        planner: BasePlanner,
        scope: ExportScope,
        conversation_id: int,
        message: str,
    ) -> dict[str, Any]:
        message = (message or "").strip()[: self.settings.ie_max_message_chars]

        # Preflight: never spend OpenAI calls (and a ~50s repair loop) when the
        # database has no CRM data to plan against — fail fast and clearly.
        readiness = compute_readiness(self.db, self.portal_id, self.settings)
        if not readiness.has_data:
            raise _no_data_error(self.portal_id)

        repo.add_message(conversation_id, role="user", content=message)

        conv = repo.get_conversation(conversation_id)
        current_plan_json = None
        if conv.current_plan_version_id:
            current_plan_json = repo.get_plan_version(conv.current_plan_version_id).plan_json

        context, available_memory = self.build_context(
            scope, repo, conversation_id, current_plan_json, message
        )
        result: PlannerResult = plan_turn(
            planner,
            db=self.db,
            portal_id=self.portal_id,
            scope=scope,
            context=context,
            message=message,
            max_repair_attempts=self.settings.ie_planner_max_repair_attempts,
        )

        response = result.response
        version_summary: dict | None = None
        validation_dict: dict | None = None
        fix_suggestions: list[str] = []
        plan_summary: dict | None = None
        llm_assistant_message = response.assistant_message

        if response.status == "validated" and result.prepared and result.prepared.plan is not None:
            plan_summary = build_plan_summary(result.prepared.plan, result.prepared.catalog)
            response.assistant_message = format_assistant_message(
                llm_assistant_message,
                plan_summary,
                status="validated",
            )
            memory_used = self._resolve_memory_used(repo, response.used_memory_ids, available_memory)
            plan_json = result.prepared.plan.model_dump(mode="json")
            plan_json.setdefault("memory_refs", [])
            for ref in memory_used:
                if ref not in plan_json["memory_refs"]:
                    plan_json["memory_refs"].append(ref)
            validation_dict = validation_to_dict(result.prepared.validation, status="valid", memory_used=memory_used)
            version = repo.save_plan_version(
                conversation_id,
                plan_json=plan_json,
                validation_result_json=validation_dict,
                catalog_snapshot_hash=result.prepared.catalog_hash,
            )
            version_summary = {
                "id": version.id,
                "version_number": version.version_number,
                "plan_hash": version.plan_hash,
            }
        elif response.status == "rejected" and result.prepared is not None:
            fix_suggestions = format_fix_suggestions(
                result.prepared.catalog, result.prepared.validation
            )
            validation_dict = validation_to_dict(
                result.prepared.validation,
                status="invalid",
                fix_suggestions=fix_suggestions,
            )
            response.assistant_message = format_assistant_message(
                (
                    f"{llm_assistant_message.strip()}\n\n{format_repair_failure_message(self.settings.ie_planner_max_repair_attempts)}"
                    if llm_assistant_message.strip()
                    else format_repair_failure_message(self.settings.ie_planner_max_repair_attempts)
                ),
                None,
                status="rejected",
                fix_suggestions=fix_suggestions,
            )

        repo.add_message(
            conversation_id,
            role="assistant",
            content=response.assistant_message,
            metadata={
                "status": response.status,
                "clarifying_questions": response.clarifying_questions,
                "fix_suggestions": fix_suggestions,
                "plan_version_id": version_summary["id"] if version_summary else None,
                "validation": validation_dict,
                "proposed_memory": [pm.model_dump() for pm in response.proposed_memory],
                "plan_summary": plan_summary,
            },
        )

        return {
            "status": response.status,
            "assistant_message": response.assistant_message,
            "clarifying_questions": response.clarifying_questions,
            "fix_suggestions": fix_suggestions,
            "plan": response.plan,
            "version": version_summary,
            "validation": validation_dict,
            "proposed_memory": [pm.model_dump() for pm in response.proposed_memory],
            "plan_summary": plan_summary,
        }

    def _resolve_memory_used(self, repo, used_ids: list[int], available_memory: list) -> list[dict]:
        by_id = {m.id: m for m in available_memory}
        refs: list[dict] = []
        for mid in used_ids:
            entry = by_id.get(mid)
            if entry is None:
                continue
            if entry.kind not in ("term", "alias", "mapping", "template", "rule", "instruction", "preference"):
                continue
            refs.append(
                {
                    "memory_id": entry.id,
                    "kind": entry.kind,
                    "version": entry.version,
                    "hash": _memory_content_hash(entry),
                }
            )
        return refs
