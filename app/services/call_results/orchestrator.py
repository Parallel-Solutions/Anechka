"""Orchestrate call result import processing."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_call_result_storage_dir, get_call_results_model
from app.models import BitrixPreparedAction, CallResultImport, CallResultImportRow, utcnow
from app.repositories.call_result_repository import CallResultRepository
from app.services.call_results.action_planner import BitrixActionPlanner
from app.services.call_results.adapters.tomoru import ADAPTER_VERSION, TomoruCallResultAdapter
from app.services.call_results.call_attempt_aggregator import (
    CallAttemptAggregator,
    build_source_identity,
    exact_duplicate_key,
    scenario_events_hash,
)
from app.services.call_results.callback_date_resolver import CallbackDateResolver
from app.services.call_results.signal_merger import MergedSignals, SignalMerger
from app.services.call_results.classification_prompt import CallResultClassificationPromptBuilder
from app.services.call_results.column_mapper import CallResultColumnMapper
from app.services.call_results.contact_number_extractor import extract_lpr_from_events
from app.services.call_results.deal_timezone_resolver import DealTimezoneResolver
from app.services.call_results.deterministic_pre_classifier import DeterministicPreClassifier, _has_content
from app.services.call_results.file_parser import CallResultFileParser, sanitize_filename
from app.services.call_results.format_detector import FormatDetector
from app.services.call_results.llm_gate import LlmGate
from app.services.call_results.llm_gateway import BaseCallResultClassifier
from app.services.call_results.llm_input_builder import LlmInputBuilder
from app.services.call_results.llm_result_validator import LLMResultValidator
from app.services.call_results.contact_marker_validator import ContactMarkerValidator
from app.services.call_results.llm_schema import CLASSIFIER_VERSION, PLANNER_VERSION, SCHEMA_VERSION, CallResultLLMResult, legacy_category_from_signals
from app.services.call_results.idempotency import build_action_idempotency_key
from app.services.call_results.matcher import CallResultMatcher
from app.services.call_results.payload_builder import BitrixPayloadBuilder
from app.services.call_results.payload_validator import BitrixPayloadValidator
from app.services.call_results.phone_normalizer import parse_phone_with_extension

logger = logging.getLogger(__name__)


def _parse_dt(val: Any) -> datetime | None:
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        return val
    text = str(val).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.replace("+03:00", "+0300")[:25], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


class CallResultOrchestrator:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        portal_id: str,
        classifier: BaseCallResultClassifier,
    ):
        self.db = db
        self.settings = settings
        self.portal_id = portal_id
        self.repo = CallResultRepository(db, portal_id)
        self.parser = CallResultFileParser()
        self.mapper = CallResultColumnMapper()
        self.tomoru = TomoruCallResultAdapter()
        self.matcher = CallResultMatcher(db, portal_id)
        self.pre_classifier = DeterministicPreClassifier()
        self.classifier = classifier
        self.input_builder = LlmInputBuilder(settings.llm_call_results_max_input_chars)
        self.llm_validator = LLMResultValidator()
        self.merger = SignalMerger()
        self.marker_validator = ContactMarkerValidator(settings)
        self.date_resolver = CallbackDateResolver()
        self.tz_resolver = DealTimezoneResolver(
            db,
            portal_id,
            fallback=getattr(settings, "call_result_timezone_fallback", "Europe/Moscow"),
        )
        self.action_planner = BitrixActionPlanner()
        self.payload_builder = BitrixPayloadBuilder()
        self.payload_validator = BitrixPayloadValidator()
        self.attempt_agg = CallAttemptAggregator()
        self.prompt_version = CallResultClassificationPromptBuilder.prompt_version

    def save_uploaded_file(
        self,
        content: bytes,
        filename: str,
        created_by: str | None = None,
        force_duplicate: bool = False,
    ) -> tuple[CallResultImport | None, dict | None]:
        sha = hashlib.sha256(content).hexdigest()
        existing = self.repo.find_by_sha256(sha)
        if existing and not force_duplicate:
            if existing.status == "uploaded":
                return existing, {
                    "duplicate": True,
                    "existing_import_id": existing.id,
                    "resumable": True,
                    "message": "Файл уже загружен — можно продолжить настройку",
                }
            return None, {
                "duplicate": True,
                "existing_import_id": existing.id,
                "message": "Этот файл уже загружался",
            }

        if len(content) > self.settings.call_result_max_file_bytes:
            raise ValueError(f"Размер файла превышает {self.settings.call_result_max_file_bytes // (1024 * 1024)} МБ")

        safe_name = sanitize_filename(filename)
        storage_dir = get_call_result_storage_dir(self.settings) / self.portal_id.replace(":", "_")
        storage_dir.mkdir(parents=True, exist_ok=True)
        import_uuid = uuid.uuid4().hex[:12]
        path = storage_dir / f"{import_uuid}_{safe_name}"
        path.write_bytes(content)
        rel_key = str(path.relative_to(get_call_result_storage_dir(self.settings)))

        rec = self.repo.create_import(
            original_filename=filename,
            storage_key=rel_key,
            file_sha256=sha,
            file_size=len(content),
            created_by=created_by,
            duplicate_of_import_id=existing.id if existing else None,
        )
        self.db.commit()
        return rec, None

    def process_import(
        self,
        import_id: int,
        *,
        sheet_name: str | None = None,
        column_mapping: dict[str, str] | None = None,
        retry_llm_only: bool = False,
    ) -> None:
        imp = self.repo.get_import(import_id)
        if imp is None:
            raise ValueError("Import not found")

        imp.status = "processing"
        imp.error_message = None
        imp.parser_version = self.settings.call_result_parser_version
        imp.planner_version = self.settings.call_result_planner_version
        imp.classifier_version = self.settings.call_result_classifier_version
        imp.adapter_version = ADAPTER_VERSION
        self.db.commit()

        try:
            if not retry_llm_only:
                self._process_full(imp, sheet_name, column_mapping)
            else:
                self._retry_llm_rows(imp)
            imp.status = "ready"
            imp.processed_at = utcnow()
            self._update_import_stats(imp)
            self.db.commit()
        except Exception as exc:
            logger.exception("Import %s failed", import_id)
            imp.status = "failed"
            imp.error_message = str(exc)
            self.db.commit()
            raise

    def _process_full(
        self,
        imp: CallResultImport,
        sheet_name: str | None,
        column_mapping: dict[str, str] | None,
    ) -> None:
        path = get_call_result_storage_dir(self.settings) / imp.storage_key
        content = path.read_bytes()
        parsed = self.parser.parse(content, imp.original_filename, sheet_name or imp.selected_sheet)
        if parsed.error:
            raise ValueError(parsed.error)

        sheet = self.parser.get_sheet(parsed, sheet_name or parsed.selected_sheet or "")
        if sheet is None:
            raise ValueError("Лист не найден")

        fmt = FormatDetector.detect(sheet.headers, imp.original_filename)
        import_warnings: list[str] = []
        if fmt.batch_meta.warning:
            import_warnings.append(fmt.batch_meta.warning)

        imp.source_format = fmt.source_format
        imp.batch_id = fmt.batch_meta.batch_id
        imp.exported_at = fmt.batch_meta.exported_at
        imp.import_warnings = import_warnings or None

        is_tomoru = fmt.is_tomoru
        if is_tomoru:
            mapping_result_mapping = fmt.auto_mapping or {}
            imp.column_mapping = mapping_result_mapping
        else:
            mapping_result = self.mapper.map_headers(sheet.headers, column_mapping or imp.column_mapping)
            if mapping_result.needs_manual and not column_mapping and not imp.column_mapping:
                if mapping_result.error:
                    raise ValueError(mapping_result.error)
            mapping_result_mapping = mapping_result.mapping
            imp.column_mapping = mapping_result_mapping

        imp.selected_sheet = sheet.name
        if len(sheet.rows) > self.settings.call_result_max_rows:
            raise ValueError(f"Строк больше лимита {self.settings.call_result_max_rows}")

        self.repo.delete_actions_for_import(imp.id)
        for old_row in self.repo.list_rows(imp.id):
            self.db.delete(old_row)
        self.db.flush()

        self.matcher.build_indexes()
        seen_hashes: set[str] = set()
        row_records: list[CallResultImportRow] = []

        batch_id = imp.batch_id
        exported_at_str = imp.exported_at.isoformat() if imp.exported_at else None

        for i, raw_row in enumerate(sheet.rows, start=2):
            warnings: list[str] = []
            if is_tomoru:
                tr = self.tomoru.normalize_row(
                    raw_row,
                    sheet.headers,
                    batch_id=batch_id,
                    exported_at=exported_at_str,
                )
                normalized = tr.normalized
                warnings.extend(tr.warnings)
            else:
                normalized = self.mapper.apply_mapping(raw_row, mapping_result_mapping)

            phone_parsed = parse_phone_with_extension(str(normalized.get("phone", "") or ""))
            if phone_parsed.multi_status == "multiple":
                normalized["phone_multi_status"] = "multiple"
                warnings.append("Несколько телефонов в поле phone")

            called_at = _parse_dt(normalized.get("called_at"))
            comment = str(normalized.get("comment") or normalized.get("content_text") or "")

            events = normalized.get("scenario_events") or []
            content_hash = scenario_events_hash(events if isinstance(events, list) else None)
            dup_hash = exact_duplicate_key(
                source_format=imp.source_format,
                batch_id=batch_id,
                normalized_phone=phone_parsed.normalized,
                last_attempt_at=called_at,
                technical_status=str(normalized.get("status") or ""),
                call_result_display=str(normalized.get("call_result") or normalized.get("technical_result") or ""),
                scenario_events=events if isinstance(events, list) else None,
                row_hash_fallback=hashlib.sha256(str(raw_row).encode()).hexdigest(),
            )
            is_dup = dup_hash in seen_hashes
            if not is_dup:
                seen_hashes.add(dup_hash)

            lpr = extract_lpr_from_events(events if isinstance(events, list) else [])
            if lpr and lpr.extension and not lpr.full_phone:
                normalized["lpr_extension"] = lpr.extension
                normalized["lpr_requires_review"] = lpr.requires_review

            file_deal_id = None
            if normalized.get("deal_id"):
                try:
                    file_deal_id = int(normalized["deal_id"])
                except (TypeError, ValueError):
                    pass

            invalid_phone = not phone_parsed.is_valid
            match = self.matcher.match_row(
                phone_parsed.normalized,
                file_deal_id,
                is_valid_phone=not invalid_phone,
            )

            pre = self.pre_classifier.classify(
                normalized,
                is_duplicate=is_dup,
                invalid_phone=invalid_phone,
            )

            source_identity = build_source_identity(
                batch_id=batch_id,
                phone=phone_parsed.normalized,
                last_attempt_at=called_at,
                content_hash=content_hash,
            )

            llm_needed = LlmGate.needs_llm(normalized, pre, llm_enabled=self.settings.llm_call_results_enabled)

            row = CallResultImportRow(
                import_id=imp.id,
                source_row_number=i,
                raw_data=raw_row,
                normalized_data=normalized,
                raw_phone=phone_parsed.raw,
                normalized_phone=phone_parsed.normalized,
                phone_extension=phone_parsed.extension or (lpr.extension if lpr else None),
                category=normalized.get("category") or normalized.get("call_result"),
                comment=comment or None,
                call_id=str(normalized.get("call_id") or "") or None,
                campaign_id=str(normalized.get("campaign_id") or "") or None,
                called_at=called_at,
                technical_status=str(normalized.get("status") or "") or None,
                call_result_display=str(normalized.get("call_result") or normalized.get("technical_result") or "") or None,
                attempts=normalized.get("attempts"),
                source_identity=source_identity,
                processing_warnings=warnings or None,
                matched_contact_id=match.matched_contact_id,
                matched_deal_id=match.matched_deal_id,
                matched_deal_local_id=match.matched_deal_local_id,
                matched_company_id=match.matched_company_id,
                match_status=match.match_status,
                match_reason=match.match_reason,
                candidate_matches=[
                    {
                        "deal_id": c.deal_id,
                        "title": c.title,
                        "assigned_by_id": c.assigned_by_id,
                        "assigned_name": c.assigned_name,
                        "local_id": c.local_id,
                    }
                    for c in match.candidates
                ] or None,
                is_duplicate=is_dup,
                row_hash=dup_hash,
                deterministic_category=pre.category,
                deterministic_reason=pre.reason,
                llm_required=llm_needed,
                llm_status="pending" if llm_needed else "not_required",
                skip_reason=pre.reason if pre.skip_bitrix else None,
            )
            row_records.append(row)

        self.repo.bulk_insert_rows(row_records)
        self.db.commit()

        self._run_llm_batch(row_records)
        for row in row_records:
            self._finalize_row(row, imp)
        self.db.commit()

    def _run_llm_batch(self, rows: list[CallResultImportRow]) -> None:
        llm_rows = [r for r in rows if r.llm_required and r.llm_status == "pending"]
        if not llm_rows:
            return

        model = get_call_results_model(self.settings)
        concurrency = max(1, self.settings.llm_call_results_concurrency)
        use_threads = concurrency > 1 and self.db.get_bind().dialect.name != "sqlite"

        if not use_threads:
            for row in llm_rows:
                row.llm_status = "processing"
                self._classify_row_with_llm(row, model)
            return

        row_ids = [r.id for r in llm_rows]
        portal_id = self.portal_id

        def _worker(row_id: int) -> None:
            from app.config import merge_db_settings
            from app.database import SessionLocal
            from app.dependencies import get_call_result_classifier_instance
            from app.services.settings_service import load_settings_from_db

            db: Session | None = SessionLocal()
            try:
                settings = merge_db_settings(load_settings_from_db(db))
                classifier = get_call_result_classifier_instance(settings)
                row = db.get(CallResultImportRow, row_id)
                if row is None:
                    return
                row.llm_status = "processing"
                db.commit()

                model = get_call_results_model(settings)
                orch = CallResultOrchestrator(db, settings, portal_id, classifier)
                bundle = orch._build_llm_bundle(row, model)
                if orch._try_llm_cache(row, bundle, model):
                    db.commit()
                    return

                payload = bundle.payload
                db.close()
                db = None

                outcome = classifier.classify(payload)

                db = SessionLocal()
                settings = merge_db_settings(load_settings_from_db(db))
                classifier = get_call_result_classifier_instance(settings)
                row = db.get(CallResultImportRow, row_id)
                if row is None:
                    return
                orch = CallResultOrchestrator(db, settings, portal_id, classifier)
                bundle = orch._build_llm_bundle(row, model)
                orch._apply_llm_outcome(row, bundle, model, outcome)
                db.commit()
            finally:
                if db is not None:
                    db.close()

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(_worker, row_ids))

        for row in llm_rows:
            self.db.refresh(row)

    def _crm_context_for_row(self, row: CallResultImportRow) -> dict[str, Any]:
        ctx: dict[str, Any] = {
            "contact_name": None,
            "deal_title": None,
            "region": None,
        }
        if row.matched_deal_id:
            if self.matcher._deals_by_id is None:
                self.matcher.build_indexes()
            deal = self.matcher.get_deal(row.matched_deal_id)
            if deal:
                ctx["deal_title"] = deal.title
        return ctx

    def _build_llm_bundle(self, row: CallResultImportRow, model: str):
        crm_ctx = self._crm_context_for_row(row)
        bundle = self.input_builder.build(
            row.normalized_data,
            prompt_version=self.prompt_version,
            schema_version=SCHEMA_VERSION,
            model=model,
            crm_context=crm_ctx,
        )
        row.llm_input_hash = bundle.input_hash
        row.llm_input_truncated = bundle.truncated
        row.llm_prompt_version = self.prompt_version
        row.llm_schema_version = SCHEMA_VERSION
        row.llm_model = model
        return bundle

    def _try_llm_cache(self, row: CallResultImportRow, bundle, model: str) -> bool:
        if not self.settings.llm_call_results_cache_enabled:
            return False
        cached = self.repo.get_llm_cache(bundle.input_hash)
        if cached and cached.prompt_version == self.prompt_version and cached.model == model:
            row.llm_result = cached.result_json
            row.llm_confidence = cached.confidence
            row.llm_status = "completed"
            row.llm_provider = "cache"
            row.llm_token_usage = cached.token_usage
            row.llm_category = legacy_category_from_signals(
                CallResultLLMResult.model_validate(cached.result_json).to_signals()
            ) if cached.result_json else None
            return True
        return False

    def _apply_llm_outcome(self, row: CallResultImportRow, bundle, model: str, outcome) -> None:
        row.llm_provider = outcome.provider
        row.llm_duration_ms = outcome.duration_ms
        row.llm_token_usage = outcome.token_usage
        row.llm_error_type = outcome.error_type

        if outcome.result is None:
            row.llm_status = (
                "failed"
                if outcome.error_type in ("timeout", "rate_limit", "error", "config", "disabled")
                else "invalid"
            )
            if outcome.error_message:
                row.llm_validation_errors = [outcome.error_message]
            return

        validation = self.llm_validator.validate(
            outcome.result,
            row.normalized_data,
            substantial_truncation=bundle.substantial_loss,
        )
        if not validation.valid or validation.result is None:
            row.llm_status = "invalid"
            row.llm_validation_errors = validation.errors + validation.warnings
            return

        row.llm_result = validation.result.model_dump(mode="json")
        row.llm_confidence = validation.result.confidence
        row.llm_category = legacy_category_from_signals(validation.result.to_signals())
        row.llm_status = "completed"
        if validation.warnings:
            existing = list(row.processing_warnings or [])
            existing.extend(validation.warnings)
            row.processing_warnings = existing

        if self.settings.llm_call_results_cache_enabled:
            self.repo.upsert_llm_cache(
                input_hash=bundle.input_hash,
                prompt_version=self.prompt_version,
                schema_version=SCHEMA_VERSION,
                model=model,
                result_json=row.llm_result,
                confidence=row.llm_confidence or 0,
                token_usage=outcome.token_usage,
            )

    def _classify_row_with_llm(self, row: CallResultImportRow, model: str) -> None:
        bundle = self._build_llm_bundle(row, model)
        if self._try_llm_cache(row, bundle, model):
            return
        outcome = self.classifier.classify(bundle.payload)
        self._apply_llm_outcome(row, bundle, model, outcome)

    def _finalize_row(self, row: CallResultImportRow, imp: CallResultImport) -> None:
        nd = row.normalized_data or {}
        pre = self.pre_classifier.classify(
            nd,
            is_duplicate=row.is_duplicate,
            invalid_phone=not bool(row.normalized_phone),
        )
        row.deterministic_category = pre.category
        row.deterministic_reason = pre.reason
        row.llm_required = LlmGate.needs_llm(nd, pre, llm_enabled=self.settings.llm_call_results_enabled)
        row.skip_reason = pre.reason if pre.skip_bitrix else None
        if not row.llm_required and row.llm_status == "pending":
            row.llm_status = "not_required"

        llm_obj = None
        if row.llm_result:
            try:
                llm_obj = CallResultLLMResult.model_validate(row.llm_result)
            except Exception:
                pass

        manual_signals = None
        if row.manually_overridden and row.business_signals:
            from app.services.call_results.signal_merger import SignalMerger
            manual_signals = SignalMerger.signals_from_dict(row.business_signals)

        match_manual = row.match_status in ("ambiguous", "conflict", "not_found", "invalid")
        merged = self.merger.merge(
            pre,
            llm_obj,
            confidence_threshold=self.settings.llm_call_results_confidence_threshold,
            llm_valid=row.llm_status == "completed",
            substantial_truncation=row.llm_input_truncated and row.llm_status == "invalid",
            match_requires_manual=match_manual,
            match_status=row.match_status,
            match_reason=row.match_reason,
            manual_signals=manual_signals,
            normalized_data=nd,
        )

        if row.is_duplicate:
            from app.services.call_results.llm_schema import CallResultSignals
            dup_sig = CallResultSignals(needs_manual_review=True, manual_review_reason="Точный дубликат попытки")
            merged = MergedSignals(
                signals=dup_sig,
                primary_outcome="manual_review",
                final_category="unknown",
                classification_source="deterministic",
                classification_reason="Точный дубликат попытки",
                requires_manual=True,
            )

        row.business_signals = merged.signals.to_dict()
        row.primary_outcome = merged.primary_outcome
        row.needs_manual_review = (
            merged.requires_manual
            or merged.signals.needs_manual_review
            or match_manual
        )
        row.manual_review_reason = (
            merged.signals.manual_review_reason
            or merged.merge_conflict_reason
            or (row.match_reason if match_manual else None)
        )
        row.final_category = merged.final_category
        row.classification_source = merged.classification_source
        row.classification_reason = merged.classification_reason
        row.extracted_data = merged.extracted_data
        row.llm_category = merged.llm_category
        row.merge_conflict_reason = merged.merge_conflict_reason
        row.row_classifier_version = CLASSIFIER_VERSION
        row.row_planner_version = PLANNER_VERSION

        nd = row.normalized_data or {}
        file_callback = _parse_dt(nd.get("callback_at"))
        tz_res = self.tz_resolver.resolve_for_deal(row.matched_deal_local_id)

        if file_callback:
            row.callback_at = file_callback
        elif merged.signals.callback_text:
            resolved = self.date_resolver.resolve(
                merged.signals.callback_text,
                merged.signals.callback_at,
                row.called_at,
                timezone=tz_res.timezone,
            )
            row.callback_at = resolved.callback_at
            if resolved.is_ambiguous:
                row.needs_manual_review = True
                row.manual_review_reason = row.manual_review_reason or "Неоднозначная дата перезвона"

        self.repo.delete_actions_for_row(row.id, preserve_user_modified=True)

        if merged.unsupported_outcome:
            row.skip_reason = merged.classification_reason
            row.execution_status = "blocked_manual_review"
            return

        if merged.requires_manual or row.is_duplicate or match_manual:
            if row.is_duplicate:
                row.skip_reason = "Точный дубликат попытки"
            elif match_manual:
                row.skip_reason = row.skip_reason or row.match_reason or "Проблема сопоставления со сделкой"
            row.execution_status = "blocked_manual_review"
            return

        deal_id = row.matched_deal_id
        deal = self.matcher.get_deal(deal_id) if deal_id else None
        assigned = deal.assigned_by_id if deal else None

        planned = self.action_planner.plan(
            row,
            bitrix_deal_id=deal_id,
            assigned_by_id=assigned,
            signals=merged.signals,
            requires_manual=merged.requires_manual,
            contact_creation_allowed=self.marker_validator.contact_creation_allowed(),
        )

        if not planned and not merged.signals.active_signal_count():
            row.skip_reason = row.skip_reason or "Нет утверждённых действий"
            row.execution_status = "prepared"
            return

        group_id = str(uuid.uuid4())
        source_id = row.source_identity or row.row_hash or str(row.id)
        for pa in planned:
            payload = pa.payload
            if pa.method in ("crm.timeline.comment.add", "crm.activity.todo.add"):
                payload = self.payload_builder.build(
                    pa,
                    row,
                    bitrix_deal_id=deal_id or 0,
                    assigned_by_id=assigned,
                    service_user_id=self.settings.bitrix_service_user_id,
                    campaign_label=imp.original_filename,
                    deadline=row.callback_at,
                    settings=self.settings,
                )
            pv = self.payload_validator.validate(pa.method, payload)
            idem = build_action_idempotency_key(
                method=pa.method,
                deal_id=deal_id,
                source_id=source_id,
                operation_type=pa.operation_type,
            )
            action = BitrixPreparedAction(
                import_id=imp.id,
                import_row_id=row.id,
                action_group_id=group_id,
                method=pa.method,
                action_type=pa.action_type,
                operation_type=pa.operation_type,
                payload=payload,
                human_summary=pa.human_summary,
                validation_status=pv.status,
                validation_errors=pv.errors or None,
                is_enabled=pa.is_enabled and pv.status != "invalid",
                idempotency_key=idem,
                sort_order=pa.sort_order,
                execution_status="prepared",
            )
            self.db.add(action)

        row.execution_status = "prepared" if not row.needs_manual_review else "blocked_manual_review"

    def _row_needs_llm_retry(self, row: CallResultImportRow) -> bool:
        if row.llm_status in ("failed", "invalid", "pending", "processing"):
            if row.llm_status in ("pending", "processing"):
                return row.llm_required or _has_content(row.normalized_data or {})
            return True
        if row.llm_status == "not_required" and _has_content(row.normalized_data or {}):
            return True
        return False

    def _prepare_row_for_llm_retry(self, row: CallResultImportRow) -> None:
        row.llm_required = True
        row.llm_status = "pending"
        row.llm_result = None
        row.llm_confidence = None
        row.llm_category = None
        row.llm_provider = None
        row.llm_validation_errors = None
        row.llm_error_type = None

    def _retry_llm_rows(self, imp: CallResultImport) -> None:
        all_rows = self.repo.list_rows(imp.id)
        llm_rows = [r for r in all_rows if self._row_needs_llm_retry(r)]
        finalize_rows = {
            r.id: r
            for r in all_rows
            if r.llm_status == "completed" and r.llm_result
        }
        if not llm_rows and not finalize_rows:
            return
        if self.matcher._deals_by_id is None:
            self.matcher.build_indexes()
        for row in llm_rows:
            self._prepare_row_for_llm_retry(row)
        self.db.commit()
        if llm_rows:
            self._run_llm_batch(llm_rows)
            for row in llm_rows:
                finalize_rows[row.id] = row
        for row in finalize_rows.values():
            self._finalize_row(row, imp)
        self.db.commit()

    def _update_import_stats(self, imp: CallResultImport) -> None:
        rows = self.repo.list_rows(imp.id)
        actions = self.repo.list_actions(imp.id)
        imp.total_rows = len(rows)
        imp.matched_rows = sum(1 for r in rows if r.match_status == "matched")
        imp.review_rows = sum(
            1 for r in rows
            if r.match_status in ("ambiguous", "conflict", "not_found")
            or r.final_category == "unknown"
            or (r.llm_confidence is not None and r.llm_confidence < self.settings.llm_call_results_confidence_threshold)
        )
        imp.skipped_rows = sum(1 for r in rows if r.skip_reason)
        imp.llm_rows_total = sum(1 for r in rows if r.llm_required)
        imp.llm_rows_completed = sum(1 for r in rows if r.llm_status == "completed")
        imp.llm_rows_failed = sum(1 for r in rows if r.llm_status in ("failed", "invalid"))
        imp.llm_rows_cached = sum(1 for r in rows if r.llm_provider == "cache")
        imp.llm_rows_skipped = sum(1 for r in rows if r.llm_status == "not_required")
        imp.llm_rows_low_confidence = sum(
            1 for r in rows
            if r.llm_confidence is not None
            and r.llm_confidence < self.settings.llm_call_results_confidence_threshold
        )
        imp.deterministic_classified = sum(1 for r in rows if r.classification_source == "deterministic")
        imp.llm_total_tokens = sum((r.llm_token_usage or {}).get("total", 0) for r in rows)

    def rebuild_row(self, import_id: int, row_id: int) -> CallResultImportRow | None:
        imp = self.repo.get_import(import_id)
        row = self.repo.get_row(import_id, row_id)
        if imp is None or row is None:
            return None
        if self.matcher._deals_by_id is None:
            self.matcher.build_indexes()
        self._finalize_row(row, imp)
        self._update_import_stats(imp)
        self.db.commit()
        return row
