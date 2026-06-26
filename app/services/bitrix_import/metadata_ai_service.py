"""OpenAI metadata analysis for Bitrix CRM fields and dictionaries."""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import OpenAI
from sqlalchemy.orm import Session

from app.config import Settings, get_metadata_model
from app.models import CrmFieldDefinition, CrmFieldSemantic
from app.repositories.crm_repository import CrmRepository
from app.utils.anonymize import anonymize_string, numeric_stats, string_stats
from app.utils.hash_utils import source_hash

logger = logging.getLogger(__name__)

BITRIX_METADATA_PROMPT_VERSION = "1"

SYSTEM_PROMPT = """Ты анализируешь метаданные CRM Bitrix24 для построения аналитического каталога данных.
Используй только переданные метаданные и обезличенные статистические примеры.
Не придумывай бизнес-смысл, если он не следует из названия, типа, настроек или значений.
Сохраняй исходные идентификаторы. Отвечай только по указанной JSON Schema.
Описания должны быть на русском языке, понятными аналитику, который не знаком с внутренними кодами Bitrix24.
Если значение неоднозначно, снижай confidence, устанавливай needs_review=true и объясняй неоднозначность в warnings.
Для каждого поля, у которого есть непустые значения, обязательно дай короткое и подробное описание. Если по имени, типу и значениям смысл неоднозначен — всё равно опиши, что видно из значений, поставь needs_review=true и понизь confidence; не оставляй описание пустым."""

FIELD_SCHEMA = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field_code": {"type": "string"},
                    "display_name": {"type": "string"},
                    "short_description": {"type": "string"},
                    "detailed_description": {"type": "string"},
                    "business_purpose": {"type": "string"},
                    "normalized_data_type": {"type": "string"},
                    "data_category": {
                        "type": "string",
                        "enum": [
                            "identifier", "status", "classification", "financial",
                            "date", "contact", "relation", "technical", "free_text", "other",
                        ],
                    },
                    "is_dictionary": {"type": "boolean"},
                    "dictionary_kind": {
                        "type": "string",
                        "enum": [
                            "enumeration", "status", "stage", "user", "currency",
                            "boolean", "relation", "inferred", "none",
                        ],
                    },
                    "nullable_description": {"type": "string"},
                    "confidence": {"type": "number"},
                    "needs_review": {"type": "boolean"},
                    "warnings": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "field_code", "display_name", "short_description", "detailed_description",
                    "business_purpose", "normalized_data_type", "data_category", "is_dictionary",
                    "dictionary_kind", "nullable_description", "confidence", "needs_review", "warnings",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["fields"],
    "additionalProperties": False,
}


class BitrixMetadataAIService:
    MAX_FIELDS_PER_REQUEST = 24
    MAX_INPUT_CHARS = 48000  # ~12000 tokens conservative

    def __init__(self, settings: Settings, db: Session, portal_id: str):
        self.settings = settings
        self.db = db
        self.portal_id = portal_id
        self.repo = CrmRepository(db, portal_id)
        self.client = OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None
        self.model = get_metadata_model(settings)
        self.ai_requests_count = 0

    def analyze_fields(
        self,
        fields: list[CrmFieldDefinition],
        value_samples: dict[int, list[str]] | None = None,
        value_profiles: dict[int, Any] | None = None,
        force: bool = False,
    ) -> int:
        if not self.client:
            logger.info("OpenAI not configured, skipping metadata analysis")
            return 0
        processed = 0
        batch: list[dict[str, Any]] = []
        batch_field_ids: list[int] = []
        batch_chars = 0

        for field in fields:
            profile = (value_profiles or {}).get(field.id)
            samples = (value_samples or {}).get(field.id, [])
            if not samples and profile is not None:
                samples = list(profile.sample_values or [])
            ctx = self._build_field_context(field, samples, profile)
            sh = self._compute_source_hash(field, ctx, profile)
            existing = self.repo.get_semantic(field.id)
            if not force and existing and existing.source_hash == sh and not existing.is_manual:
                continue
            if existing and existing.is_manual:
                continue
            ctx_json = json.dumps(ctx, ensure_ascii=False)
            if (
                len(batch) >= self.MAX_FIELDS_PER_REQUEST
                or batch_chars + len(ctx_json) > self.MAX_INPUT_CHARS
            ):
                processed += self._send_batch(batch, batch_field_ids, value_profiles)
                batch, batch_field_ids, batch_chars = [], [], 0
            batch.append(ctx)
            batch_field_ids.append(field.id)
            batch_chars += len(ctx_json)

        if batch:
            processed += self._send_batch(batch, batch_field_ids, value_profiles)
        return processed

    def _build_field_context(
        self,
        field: CrmFieldDefinition,
        samples: list[str],
        profile: Any | None = None,
    ) -> dict[str, Any]:
        anon_samples = [anonymize_string(s) for s in samples[:20]]
        ctx: dict[str, Any] = {
            "entity_type_id": field.entity_type_id,
            "field_code": field.original_field_name,
            "title": field.title,
            "list_label": field.list_label,
            "form_label": field.form_label,
            "filter_label": field.filter_label,
            "field_type": field.field_type,
            "is_multiple": field.is_multiple,
            "is_required": field.is_required,
            "is_read_only": field.is_read_only,
            "is_custom": field.is_custom,
            "settings": field.settings,
        }
        if profile is not None:
            ctx["value_stats"] = {
                "filled_count": profile.filled_count,
                "null_count": profile.null_count,
                "distinct_count": profile.distinct_count,
                "observed_types": profile.observed_types,
            }
            if profile.numeric_stats:
                ctx["numeric_stats"] = profile.numeric_stats
            if profile.length_stats:
                ctx["length_stats"] = profile.length_stats
            ctx["examples"] = list(profile.sample_values or [])[:20]
        elif anon_samples:
            ctx["value_stats"] = string_stats(samples)
            ctx["examples"] = anon_samples
        return ctx

    def _compute_source_hash(
        self, field: CrmFieldDefinition, ctx: dict, profile: Any | None = None
    ) -> str:
        return source_hash(
            field.definition_hash,
            ctx,
            profile.value_signature if profile is not None else "",
            BITRIX_METADATA_PROMPT_VERSION,
            self.model,
        )

    def _send_batch(
        self,
        contexts: list[dict],
        field_ids: list[int],
        value_profiles: dict[int, Any] | None = None,
    ) -> int:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps({"fields": contexts}, ensure_ascii=False),
                    },
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "field_semantics",
                        "strict": True,
                        "schema": FIELD_SCHEMA,
                    },
                },
                store=False,
            )
            self.ai_requests_count += 1
            content = response.choices[0].message.content or "{}"
            parsed = json.loads(content)
            results = parsed.get("fields", [])
            count = 0
            for idx, result in enumerate(results):
                if idx >= len(field_ids):
                    break
                field_id = field_ids[idx]
                field = self.db.get(CrmFieldDefinition, field_id)
                if not field:
                    continue
                ctx = contexts[idx]
                sh = self._compute_source_hash(
                    field, ctx, (value_profiles or {}).get(field_id)
                )
                self.repo.save_semantic(
                    field_id,
                    {
                        "language": "ru",
                        "display_name": result.get("display_name"),
                        "short_description": result.get("short_description"),
                        "detailed_description": result.get("detailed_description"),
                        "business_purpose": result.get("business_purpose"),
                        "normalized_data_type": result.get("normalized_data_type"),
                        "data_category": result.get("data_category"),
                        "is_dictionary": result.get("is_dictionary", False),
                        "dictionary_kind": result.get("dictionary_kind"),
                        "nullable_description": result.get("nullable_description"),
                        "confidence": result.get("confidence"),
                        "needs_review": result.get("needs_review", True),
                        "warnings": result.get("warnings", []),
                        "source_hash": sh,
                        "prompt_version": BITRIX_METADATA_PROMPT_VERSION,
                        "model_name": self.model,
                        "generated_at": __import__("app.models", fromlist=["utcnow"]).utcnow(),
                    },
                )
                count += 1
            self.db.commit()
            return count
        except Exception as exc:
            logger.exception("AI metadata analysis failed: %s", exc)
            self.db.rollback()
            return 0

    @staticmethod
    def validate_field_response(data: dict) -> bool:
        required = FIELD_SCHEMA["properties"]["fields"]["items"]["required"]
        for key in required:
            if key not in data:
                return False
        return True
