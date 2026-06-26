"""ExportPlanPlanner — turns natural language into a *candidate* ExportPlan 2.0.

The AI is strictly a planner: it never executes anything, never writes SQL /
Python / JSONPath, never invents field codes, never changes the data scope and
never publishes memory on its own. Its only job is to emit a JSON candidate
plan (or clarifying questions). Safety is enforced entirely server-side:
JSON parse -> Pydantic -> structural validator -> catalog validator -> scope
validator. A deterministic FakePlanner is used in tests (no network).
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.config import Settings, get_planner_model
from app.services.export_plan.catalog import FieldCatalog
from app.services.export_plan.plan_normalizer import normalize_llm_plan
from app.services.export_plan.validator import ExportScope
from app.services.intelligent_export.date_tokens import resolve_date_tokens
from app.services.intelligent_export.plan_enricher import enrich_plan
from app.services.intelligent_export.tomoru_stages import KpStageCatalog
from app.services.intelligent_export.plan_service import (
    PreparedPlan,
    enrich_issues,
    prepare_plan,
)

logger = logging.getLogger(__name__)

PLANNER_PROMPT_VERSION = "4"

PlannerStatus = Literal["needs_clarification", "candidate_ready", "validated", "rejected"]


class ProposedMemory(BaseModel):
    model_config = {"extra": "ignore"}
    scope: str = "project"
    kind: str = "term"
    key: str = ""
    content: str = ""


class PlannerResponse(BaseModel):
    model_config = {"extra": "ignore"}
    status: PlannerStatus = "needs_clarification"
    assistant_message: str = ""
    clarifying_questions: list[str] = Field(default_factory=list)
    plan: dict[str, Any] | None = None
    proposed_memory: list[ProposedMemory] = Field(default_factory=list)
    used_memory_ids: list[int] = Field(default_factory=list)


SYSTEM_PROMPT = """Ты — планировщик выгрузок данных CRM (ExportPlan 2.0). Твоя ЕДИНСТВЕННАЯ
задача — по запросу пользователя на естественном языке составить КАНДИДАТА плана
выгрузки в формате JSON или задать уточняющие вопросы. Ты НИЧЕГО не исполняешь.

СТРОГО ЗАПРЕЩЕНО:
- писать SQL, Python, JSONPath, любые выражения или код;
- придумывать коды полей (field_code), entity_type_id, справочников или связей —
  используй ТОЛЬКО значения из переданного каталога (context.catalog), реестра связей
  (context.registry.relations), преобразований (transforms) и правил (validation_rules);
- менять область доступа (scope), фильтр по ответственному, лимиты сверх scope;
- самостоятельно «сохранять» или «публиковать» память — можешь лишь предложить записи
  в proposed_memory, их подтверждает человек;
- выдумывать абсолютные даты. Для относительных периодов используй токены:
  @today, @today-30d, @month_start, @month_end, @prev_month_start, @prev_month_end,
  @year_start, @year_end. Сервер сам подставит конкретные даты.

СХЕМА ExportPlan 2.0 (ключевые поля):
- schema_version="2.0", title (обязателен), description (необязателен).
- datasets[]: { id, primary_entity_type_id, sources[]{alias, entity_type_id},
  relation_refs[]{relation_code, from_alias, to_alias}, filters[], sort[], limit }.
  primary_entity_type_id ОБЯЗАН совпадать с entity_type_id одного из sources.
  Соединения других сущностей — ТОЛЬКО через relation_refs с relation_code из реестра.
- workbook: { format("xlsx"|"csv"), filename_label, sheets[] }.
- sheets[]: { id, name(<=31 симв), mode, dataset_id, columns[], row_filters[], sort[],
  group_by[], aggregates[], validation_rules[], error_policy }.
  mode: "rows" (строки), "aggregate" (требует group_by или aggregates), "errors"
  (строки с ошибками валидации), "parameters" (лист параметров, dataset_id не нужен).
- columns[]: { id, header, value, transforms[] }. value.kind — одно из:
  - "field": {kind, field:{entity_type_id, field_code, source_alias}}
  - "constant": {kind, value}
  - "concat": {kind, parts:[ValueAtom], separator}
  - "coalesce": {kind, parts:[field|constant|concat]} — первый непустой
  - "conditional": {kind, cases:[{when:Condition, then:ValueAtom}], default}
  - "aggregate": {kind, func:"count|sum|avg|min|max", field?}
  (ValueAtom — это только field или constant.)
- sort[] (в dataset и sheet): { field:{entity_type_id, field_code, source_alias},
  direction:"asc"|"desc" }. В sort используется direction, НЕ op — op только в filters
  и conditions.
- Condition/filter: { field:{entity_type_id, field_code, source_alias}, op, value|values }.
  op из allowed_filter_ops поля. Для in/not_in задавай values[], для is_null/is_not_null
  значение не нужно, иначе задавай value.
- transforms[]: { op, params } — op только из реестра transforms.
- validation_rules[]: { id, type, column_id|field, params, severity }.

ПРАВИЛА МАППИНГА И КАЧЕСТВА:
- Сопоставляй формулировки пользователя с display_name/description полей каталога;
  бери поле с подходящим entity_type_id. НИКОГДА не выдумывай field_code.
- Для enum/status/user-полей (есть dictionary_code) добавляй transform "dictionary_label",
  чтобы в выгрузку попадали человекочитаемые значения, а не коды.
- Для телефонов используй "phone_normalize", для дат — "date_format",
  для денежных/числовых сумм — "number_round".
- Для телефона/почты бери ТИПИЗИРОВАННЫЕ поля каталога "PHONE"/"EMAIL", а НЕ общий
  контейнер мультиполей "FM": "FM" возвращает первый элемент (часто это e-mail), и
  "phone_normalize" над ним даст ошибку. "phone_normalize" применяй только к "PHONE".
- Давай осмысленные header колонок на русском и стабильные id (латиница, snake_case).
- Если запрос типовой ("выгрузи сделки", "все контакты") — НЕ задавай лишних вопросов,
  а собери разумный набор колонок по умолчанию: идентификатор, название, стадия/статус,
  сумма (для сделок), даты создания/изменения, ответственный. Допущения коротко опиши
  в assistant_message.
- Чувствительные поля (sensitive=true, напр. PHONE/EMAIL) добавляй только если они явно
  запрошены и роль это позволяет (scope.allow_sensitive_fields=true).
- Для КОНТАКТОВ сделок/лидов используй relation_code "deal_contact_link" /
  "lead_contact_link" (а для одного основного — "deal_primary_contact_link" /
  "lead_primary_contact_link"): это реальные привязки CRM из crm_contact_links и
  подтягивают ВСЕ контакты (одна сделка может дать несколько строк — это нормально).
  Устаревшие "deal_contact" / "lead_contact" соединяют по contactId из payload,
  который чаще всего пуст — НЕ используй их для контактов сделок/лидов.

КОНТАКТЫ — ОБЯЗАТЕЛЬНЫЙ МАППИНГ:
- «имя»/«ФИО»/«контакт» → value kind "coalesce": concat(LAST_NAME, NAME, SECOND_NAME)
  с separator " " как первый part, TITLE как fallback; НЕ используй TITLE как единственный
  источник имени.
- «должность» → POST (или UF-поле из catalog с display_name «должность»).
- «описание контакта» → COMMENTS.
- «телефон» → PHONE + transform phone_normalize; НЕ FM.
- Контакты сделок/лидов → relation_code deal_contact_link / lead_contact_link.
- НЕ добавляй validation_rules (required, not_empty_after_transform), если пользователь
  явно не просит «только с телефоном», «без пустых», «обязательно заполнено».

ВЫГРУЗКА ДЛЯ TOMORU / ОБЗВОНА:
- Если пользователь просит выгрузку для Tomoru, обзвона или список номеров для обзвона —
  строй plan только по СДЕЛКАМ (без join контактов): filters/sort/limit на dataset сделок.
- Обязательно исключи архивные сделки: фильтр CLOSED eq "N" (если поле CLOSED есть в каталоге).
- Только воронка «Коммерческое предложение»: фильтр CATEGORY_ID eq 15 (не предлагай другие воронки).
- Регион/город (Москва=1105, Санкт-Петербург=1107, Томск=1091 и т.п.) — фильтр UF_CRM_5ECE25C5D78E0 eq <числовой ID>
  или не добавляй фильтр региона вовсе (сервер подставит сам по тексту запроса).
  НИКОГДА не используй placeholder вида «<ID региона …>» — только число или пропуск фильтра.
- Название стадии (Новая, Тёплый, длинные kanban-имена) резолвится сервером по справочнику всех воронок (БД + Bitrix API).
  В plan можно не дублировать STAGE_ID; если указываешь — точный код из нужной воронки ("7", "C15:NEW", "C15:4", "LOSE").
  НЕ фильтруй стадию через TITLE contains.
- На листе достаточно одной колонки «Телефон»; сервер сам выберет контакт (архитектор → ЛПР →
  последний) и телефон (MOBILE → WORK) в формате 7XXXXXXXXXX. Не добавляй deal_contact_link.
- В assistant_message укажи, что контакт/телефон выбираются серверной эвристикой, архив исключён,
  воронка — Коммерческое предложение.

ФОРМАТ assistant_message (ОБЯЗАТЕЛЬНО):
assistant_message — структурированный ответ на русском (3–6 предложений):
1) Что будет выгружено (сущности, связи).
2) Какие колонки и откуда (напр. «ФИО контакта = фамилия+имя+отчество, fallback на название»).
3) Фильтры и допущения (город, период, лимит строк).
4) Особенности (несколько контактов на сделку → несколько строк).
5) Если есть риски — одной строкой («точность зависит от заполненности карточек CRM»).
ЗАПРЕЩЕНО просто перефразировать запрос пользователя одной строкой.

КОГДА УТОЧНЯТЬ:
- status="needs_clarification" (plan=null) — только при реальной неоднозначности, без
  которой план будет заведомо неверным. В остальных случаях возвращай рабочий план.
- status="candidate_ready" + полный объект plan — когда можешь предложить выгрузку.

ИСПРАВЛЕНИЕ ОШИБОК:
- Если в запросе есть previous_validation_errors — внимательно устрани КАЖДУЮ ошибку,
  опираясь на hint/suggestions (поля каталога, допустимые операторы) и верни обновлённый
  plan. Не повторяй те же ошибки.

ФОРМАТ ОТВЕТА: отвечай ТОЛЬКО валидным JSON-объектом с полями: status,
assistant_message (структурированно, на русском — см. выше), clarifying_questions,
plan, proposed_memory, used_memory_ids.

ПРИМЕР (rows):
{"status":"candidate_ready","assistant_message":"Выгрузка сделок за текущий месяц.",
"clarifying_questions":[],"plan":{"schema_version":"2.0","title":"Сделки за месяц",
"datasets":[{"id":"deals","primary_entity_type_id":2,"sources":[{"alias":"deal",
"entity_type_id":2}],"filters":[{"field":{"entity_type_id":2,"field_code":"DATE_CREATE",
"source_alias":"deal"},"op":"gte","value":"@month_start"}],"limit":5000}],"workbook":
{"format":"xlsx","sheets":[{"id":"main","name":"Сделки","mode":"rows","dataset_id":
"deals","columns":[{"id":"title","header":"Название","value":{"kind":"field","field":
{"entity_type_id":2,"field_code":"TITLE","source_alias":"deal"}}},{"id":"stage",
"header":"Стадия","value":{"kind":"field","field":{"entity_type_id":2,"field_code":
"STAGE_ID","source_alias":"deal"}},"transforms":[{"op":"dictionary_label","params":{}}]},
{"id":"amount","header":"Сумма","value":{"kind":"field","field":{"entity_type_id":2,
"field_code":"OPPORTUNITY","source_alias":"deal"}},"transforms":[{"op":"number_round",
"params":{"digits":2}}]}]}]},"proposed_memory":[],"used_memory_ids":[]}}

ПРИМЕР (aggregate — сумма сделок по стадиям):
{"status":"candidate_ready","assistant_message":"Сумма сделок по стадиям.",
"clarifying_questions":[],"plan":{"schema_version":"2.0","title":"Сделки по стадиям",
"datasets":[{"id":"deals","primary_entity_type_id":2,"sources":[{"alias":"deal",
"entity_type_id":2}],"limit":5000}],"workbook":{"format":"xlsx","sheets":[{"id":"agg",
"name":"По стадиям","mode":"aggregate","dataset_id":"deals","group_by":[{"entity_type_id":
2,"field_code":"STAGE_ID","source_alias":"deal"}],"aggregates":[{"id":"total","func":
"sum","field":{"entity_type_id":2,"field_code":"OPPORTUNITY","source_alias":"deal"}}]}]}},
"proposed_memory":[],"used_memory_ids":[]}}

ПРИМЕР (сделки + контакты + фильтр по городу):
{"status":"candidate_ready","assistant_message":"Выгрузка контактов из сделок с фильтром по Красноярску. Колонки: телефон (нормализованный), ФИО контакта (фамилия+имя+отчество, fallback на название), должность, описание. Фильтр: сделки, где название содержит «Красноярск». У одной сделки может быть несколько контактов — каждый в отдельной строке.",
"clarifying_questions":[],"plan":{"schema_version":"2.0","title":"Контакты из сделок по Красноярску",
"datasets":[{"id":"deals_contacts","primary_entity_type_id":2,"sources":[{"alias":"deal","entity_type_id":2},
{"alias":"contact","entity_type_id":3}],"relation_refs":[{"relation_code":"deal_contact_link","from_alias":"deal",
"to_alias":"contact"}],"filters":[{"field":{"entity_type_id":2,"field_code":"TITLE","source_alias":"deal"},
"op":"contains","value":"Красноярск"}],"limit":5000}],"workbook":{"format":"xlsx","sheets":[{"id":"main",
"name":"Контакты","mode":"rows","dataset_id":"deals_contacts","columns":[{"id":"phone","header":"Телефон",
"value":{"kind":"field","field":{"entity_type_id":3,"field_code":"PHONE","source_alias":"contact"}},
"transforms":[{"op":"phone_normalize","params":{}}]},{"id":"full_name","header":"Имя","value":{"kind":"coalesce",
"parts":[{"kind":"concat","parts":[{"kind":"field","field":{"entity_type_id":3,"field_code":"LAST_NAME",
"source_alias":"contact"}},{"kind":"field","field":{"entity_type_id":3,"field_code":"NAME","source_alias":"contact"}},
{"kind":"field","field":{"entity_type_id":3,"field_code":"SECOND_NAME","source_alias":"contact"}}],
"separator":" "},{"kind":"field","field":{"entity_type_id":3,"field_code":"TITLE","source_alias":"contact"}}]},
{"id":"post","header":"Должность","value":{"kind":"field","field":{"entity_type_id":3,"field_code":"POST",
"source_alias":"contact"}}},{"id":"comments","header":"Описание","value":{"kind":"field","field":
{"entity_type_id":3,"field_code":"COMMENTS","source_alias":"contact"}}}]}]}},
"proposed_memory":[],"used_memory_ids":[]}}"""


class BasePlanner(ABC):
    supports_repair: bool = False

    @abstractmethod
    def generate(self, context: dict[str, Any], message: str, prior_errors: list[dict] | None = None) -> dict[str, Any]:
        ...


class OpenAIPlanner(BasePlanner):
    supports_repair = True

    def __init__(self, settings: Settings):
        from openai import OpenAI

        if not settings.openai_api_key:
            raise RuntimeError("OpenAI API key not configured")
        self.settings = settings
        timeout = getattr(settings, "ie_planner_timeout_seconds", 30.0)
        self.client = OpenAI(api_key=settings.openai_api_key, timeout=timeout)
        self.model = get_planner_model(settings)
        self.temperature = settings.ie_planner_temperature

    def generate(self, context: dict[str, Any], message: str, prior_errors: list[dict] | None = None) -> dict[str, Any]:
        user_payload: dict[str, Any] = {"request": message, "context": context}
        if prior_errors:
            user_payload["previous_validation_errors"] = prior_errors[:30]
            user_payload["instruction"] = (
                "Исправь план так, чтобы устранить ВСЕ ошибки валидации. "
                "Опирайся на hint и suggestions у каждой ошибки."
            )
        # The context is already budgeted upstream (relevant catalog subset +
        # trimmed history), so we serialize it whole — never slice the JSON
        # string, which would corrupt the structure and degrade quality.
        content = json.dumps(user_payload, ensure_ascii=False)
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
                response_format={"type": "json_object"},
                temperature=self.temperature,
                store=False,
            )
        except Exception as exc:  # noqa: BLE001
            # Timeouts / connectivity / API errors must not surface as a 500;
            # turn them into a clear, user-facing AI_UNAVAILABLE response.
            from app.services.intelligent_export.errors import ie_error

            logger.warning("OpenAI planner call failed: %s", exc)
            raise ie_error(
                "AI_UNAVAILABLE",
                "AI планировщик не ответил вовремя или недоступен. Попробуйте ещё раз "
                "или отредактируйте план вручную / используйте быстрый экспорт.",
            ) from exc
        raw = response.choices[0].message.content or "{}"
        return json.loads(raw)


class FakePlanner(BasePlanner):
    """Deterministic planner for tests.

    Either pass a list of raw response dicts (returned in order, last repeated),
    or a callable ``fn(context, message, prior_errors) -> dict``.
    """

    def __init__(self, responses: list[dict] | None = None, fn=None, supports_repair: bool = True):
        self._responses = responses or []
        self._fn = fn
        self._idx = 0
        self.supports_repair = supports_repair
        self.calls: list[dict] = []

    def generate(self, context: dict[str, Any], message: str, prior_errors: list[dict] | None = None) -> dict[str, Any]:
        self.calls.append({"message": message, "prior_errors": prior_errors})
        if self._fn is not None:
            return self._fn(context, message, prior_errors)
        if not self._responses:
            return {"status": "needs_clarification", "assistant_message": "Уточните запрос", "clarifying_questions": ["?"]}
        idx = min(self._idx, len(self._responses) - 1)
        self._idx += 1
        return self._responses[idx]


@dataclass
class PlannerResult:
    response: PlannerResponse
    prepared: PreparedPlan | None


_MALFORMED_RESPONSE_MESSAGE = (
    "Не удалось разобрать ответ планировщика. Попробуйте переформулировать запрос."
)


def _normalize_planner_raw(raw: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(raw)
    if normalized.get("assistant_message") is None:
        normalized["assistant_message"] = ""
    for key in ("clarifying_questions", "proposed_memory", "used_memory_ids"):
        if normalized.get(key) is None:
            normalized[key] = []
    return normalized


def _parse_planner_response(raw: dict[str, Any]) -> PlannerResponse:
    try:
        return PlannerResponse.model_validate(_normalize_planner_raw(raw))
    except ValidationError:
        logger.exception("Planner response failed validation: %r", raw)
        return PlannerResponse(
            status="needs_clarification",
            assistant_message=_MALFORMED_RESPONSE_MESSAGE,
        )


def plan_turn(
    planner: BasePlanner,
    *,
    db: Session,
    portal_id: str,
    scope: ExportScope,
    context: dict[str, Any],
    message: str,
    max_repair_attempts: int = 1,
) -> PlannerResult:
    today = date.fromisoformat(context["today"])

    raw = planner.generate(context, message)
    response = _parse_planner_response(raw)

    if response.plan is None or response.status == "needs_clarification":
        response.status = "needs_clarification"
        return PlannerResult(response=response, prepared=None)

    catalog = FieldCatalog.load(db, portal_id)
    kp_stages = KpStageCatalog.load(db, portal_id)
    enrichment_warnings: list[str] = []

    def _prepare_from_raw(raw_plan: dict) -> tuple[dict, PreparedPlan]:
        enrichment_warnings.clear()
        plan_dict = normalize_llm_plan(resolve_date_tokens(raw_plan, today))
        plan_dict = enrich_plan(
            plan_dict,
            user_message=message,
            catalog=catalog,
            kp_stages=kp_stages,
            enrichment_warnings=enrichment_warnings,
        )
        return plan_dict, prepare_plan(db, portal_id, scope, plan_dict)

    plan_dict, prepared = _prepare_from_raw(response.plan)

    # Iterative self-repair: feed validation errors (enriched with catalog
    # suggestions and allowed operators) back to the planner until the plan
    # validates or attempts are exhausted. Keep the latest candidate so the
    # rejection surfaced to the user reflects the most recent attempt.
    attempts = max(0, max_repair_attempts) if getattr(planner, "supports_repair", False) else 0
    for _ in range(attempts):
        if prepared.valid:
            break
        errors = enrich_issues(prepared.validation, prepared.catalog)
        raw_next = planner.generate(context, message, errors)
        response_next = _parse_planner_response(raw_next)
        if response_next.plan is None:
            break
        plan_dict_next, prepared_next = _prepare_from_raw(response_next.plan)
        response, prepared, plan_dict = response_next, prepared_next, plan_dict_next

    if enrichment_warnings:
        response.assistant_message = (
            response.assistant_message.rstrip() + "\n\n" + "\n".join(enrichment_warnings)
        ).strip()
    response.plan = plan_dict
    response.status = "validated" if prepared.valid else "rejected"
    return PlannerResult(response=response, prepared=prepared)
