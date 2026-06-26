"""OpenAI chat with Bitrix24 function-calling tools."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI
from sqlalchemy.orm import Session

from app.config import Settings, get_export_dir
from app.exceptions import AppError, BitrixAuthenticationError
from app.models import ExportJob, utcnow
from app.services.bitrix_client import BitrixClient
from app.services.export_service import ExportStatistics
from app.services.job_service import JobService
from app.services.lpr_service import load_lpr_config
from app.services.lpr_tomoru_service import LprTomoruService

logger = logging.getLogger(__name__)

DEFAULT_REGION_IBLOCK_ID = 49

MAX_TOOL_ITERATIONS = 10
DEFAULT_SELECT = ["ID", "TITLE"]

SYSTEM_PROMPT = """Ты — ассистент для работы с данными Bitrix24 CRM в приложении выгрузки.

Твои возможности:
1. Искать сделки, контакты и компании через Bitrix REST API (live-данные, не локальная БД).
2. Получать справочники: категории воронок, стадии, пользователи, поля сущностей.
3. Запускать фоновые задачи импорта/выгрузки в Excel (region, stage, category_full).

Правила:
- Отвечай на русском языке.
- Перед поиском при необходимости вызывай get_entity_fields, list_categories, list_stages, list_users для уточнения кодов полей и ID.
- Фильтры Bitrix передавай как JSON-объект (например {"CATEGORY_ID": 15, "STAGE_ID": "C15:NEW"}).
- Для неравенства используй префикс "!" (например {"!ASSIGNED_BY_ID": 1}).
- select — список кодов полей; если пользователь не указал поля, выбирай релевантный минимальный набор.
- limit не превышай max_export_size; по умолчанию используй разумный лимит (50–200).
- Если данных много, предупреди пользователя и покажи первые строки.
- Для выгрузки в Excel используй start_export с корректным mode и params.
- После получения табличных данных кратко опиши результат и предложи скачать Excel.

Режимы start_export:
- region: region_name, region_id, category_id, iblock_id, region_field, limit
- stage: category_id, stage_id, limit, excluded_user_ids, excel_format (normalized|wide)
- category_full: category_id, limit, excluded_user_ids

ЛПР и выгрузка в Tomoru:
- ЛПР — лицо, принимающее решения (директор, руководитель, владелец, учредитель и т.п.).
  Признак ЛПР определяется эвристикой по полям контакта (по умолчанию «Должность»/POST)
  и редактируемому списку ключевых слов в настройках. Не угадывай — система фильтрует сама.
- Если пользователь просит «выбрать ЛПР» по региону и «подготовить результат для выгрузки
  в тумороу / Tomoru / для обзвона», вызывай инструмент export_lpr_tomoru.
- export_lpr_tomoru формирует Excel из двух листов: «Номера» (телефоны 7XXXXXXXXXX в столбик,
  только цифры — формат для загрузки в Tomoru) и «Отчёт» (человекочитаемые данные).
- Параметры export_lpr_tomoru: region_name (обяз. если нет region_id), region_id,
  category_id (default 15), limit. Регион резолвится по названию автоматически.
- После выгрузки кратко сообщи число найденных номеров и предложи скачать файл.
"""

DATA_SPEC = """## Модель данных Bitrix24 CRM

### Сущности и REST-методы
- deal (сделка) → crm.deal.list; инструмент search_deals
- contact (контакт) → crm.contact.list; инструмент search_contacts
- company (компания) → crm.company.list; инструмент search_companies

### Поля сделки (deal) — код → русские синонимы
- ID — идентификатор, номер сделки
- TITLE — название, наименование
- CATEGORY_ID — воронка, категория, направление (число; 0 = «Общая» воронка)
- STAGE_ID — стадия, этап, статус (строка формата C{category}:{CODE}, напр. C15:NEW)
- ASSIGNED_BY_ID — ответственный, менеджер, сотрудник (ID пользователя)
- OPPORTUNITY — сумма, бюджет, стоимость
- CURRENCY_ID — валюта (RUB, USD и т.д.)
- COMPANY_ID — компания, организация
- CONTACT_ID — основной контакт
- SOURCE_ID — источник, канал
- DATE_CREATE — дата создания, создана
- DATE_MODIFY — дата изменения, изменена
- CLOSEDATE — дата закрытия, закрыта
- UF_CRM_5ECE25C5D78E0 — регион (пользовательское поле; значение — ID элемента списка регионов)

### Поля контакта (contact)
- ID — идентификатор
- NAME, LAST_NAME, SECOND_NAME — имя, фамилия, отчество, ФИО
- PHONE — телефон (multifield, массив объектов)
- EMAIL — email, почта (multifield)
- COMPANY_ID — компания
- ASSIGNED_BY_ID — ответственный
- DATE_CREATE — дата создания

### Поля компании (company)
- ID — идентификатор
- TITLE — название, наименование организации
- PHONE — телефон (multifield)
- EMAIL — email (multifield)
- ASSIGNED_BY_ID — ответственный
- DATE_CREATE — дата создания

### Multifield-поля (PHONE, EMAIL)
Возвращаются массивом: [{"VALUE": "+7...", "VALUE_TYPE": "WORK|MOBILE|..."}].
В select указывай просто PHONE или EMAIL.

### Операторы фильтра (filter)
- Равенство: {"FIELD": value}
- Неравенство: {"!FIELD": value} или {"!FIELD": [1, 2]} для исключения списка
- Больше/меньше: {">FIELD": value}, {"<FIELD": value}, {">=FIELD": value}, {"<=FIELD": value}
- Подстрока (LIKE): {"%FIELD": "Москва"}
- Список значений (IN): {"FIELD": [val1, val2]}
- Диапазон дат: {">=DATE_CREATE": "2026-05-01T00:00:00", "<=DATE_CREATE": "2026-05-31T23:59:59"}

Примеры перевода запросов пользователя:
- «сделки воронки 15 на стадии NEW» → {"CATEGORY_ID": 15, "STAGE_ID": "C15:NEW"}
- «исключить ответственного 439» → {"!ASSIGNED_BY_ID": 439}
- «контакты за последний месяц» → {">=DATE_CREATE": "<ISO-дата месяц назад>"}
- «компании из Москвы» → {"%ADDRESS_CITY": "Москва"} (уточни код через get_entity_fields)

### Воронки и стадии
- CATEGORY_ID=0 — виртуальная воронка «Общая»
- STAGE_ID зависит от воронки: перед фильтрацией по стадии вызывай list_categories и list_stages
- Название стадии («Новая», «В работе») → сопоставь с id через list_stages, не угадывай код

### Справочники
- list_categories — воронки (id, name)
- list_stages(category_id) — стадии (id=STATUS_ID, name)
- list_users — активные пользователи (id, name) для ASSIGNED_BY_ID
- get_entity_fields(entity_type) — полный список полей с русскими названиями

### Ограничения
- Данные live из Bitrix REST API, не из локальной БД приложения
- ИНН, КПП и реквизиты компании не в полях company — нужен crm.requisite (не поддерживается). Сообщи пользователю, если просят ИНН
- Поиск региона по названию для выгрузки — через lists.element.get (iblock_id=49); для live-поиска сделок по региону используй UF_CRM_5ECE25C5D78E0 с ID региона
"""

FULL_SYSTEM_PROMPT = SYSTEM_PROMPT + "\n\n" + DATA_SPEC

_FILTER_DESC = (
    "JSON-объект фильтра Bitrix CRM. Операторы: равенство FIELD, "
    'неравенство "!FIELD", сравнения ">FIELD"/"<FIELD"/">=FIELD"/"<=FIELD", '
    'подстрока "%FIELD", список значений массивом. '
    'Пример сделок: {"CATEGORY_ID": 15, "STAGE_ID": "C15:NEW", "!ASSIGNED_BY_ID": 1}. '
    'Пример контактов: {">=DATE_CREATE": "2026-05-01T00:00:00"}. '
    'Пример компаний: {"%TITLE": "ООО"}.'
)

_SELECT_DEAL_DESC = (
    "Коды полей для выборки. Частые: ID, TITLE, CATEGORY_ID, STAGE_ID, "
    "ASSIGNED_BY_ID, OPPORTUNITY, CURRENCY_ID, COMPANY_ID, CONTACT_ID, "
    "SOURCE_ID, DATE_CREATE, DATE_MODIFY, CLOSEDATE, UF_CRM_5ECE25C5D78E0 (регион). "
    "Если пользователь не указал поля — минимальный релевантный набор."
)

_SELECT_CONTACT_DESC = (
    "Коды полей: ID, NAME, LAST_NAME, SECOND_NAME, PHONE, EMAIL, "
    "COMPANY_ID, ASSIGNED_BY_ID, DATE_CREATE."
)

_SELECT_COMPANY_DESC = (
    "Коды полей: ID, TITLE, PHONE, EMAIL, ASSIGNED_BY_ID, DATE_CREATE. "
    "ИНН недоступен через company — сообщи пользователю."
)

_LIMIT_DESC = "Максимум записей (1..max_export_size). По умолчанию 50–200."

_EXPORT_PARAMS_DESC = (
    "Параметры выгрузки по mode. "
    "region: region_name (обяз.), region_id (ID элемента списка), category_id (default 15), "
    "iblock_id (default 49), region_field (default UF_CRM_5ECE25C5D78E0), limit. "
    "stage: category_id, stage_id (напр. C15:NEW), limit, excluded_user_ids (массив ID), "
    "excel_format (normalized|wide). "
    "category_full: category_id, limit, excluded_user_ids."
)

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_deals",
            "description": "Поиск сделок в Bitrix24 по фильтру (crm.deal.list). Ключевые поля: CATEGORY_ID, STAGE_ID, ASSIGNED_BY_ID, OPPORTUNITY, UF_CRM_5ECE25C5D78E0.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "object",
                        "description": _FILTER_DESC,
                    },
                    "select": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": _SELECT_DEAL_DESC,
                    },
                    "limit": {
                        "type": "integer",
                        "description": _LIMIT_DESC,
                    },
                },
                "required": ["filter"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_contacts",
            "description": "Поиск контактов в Bitrix24 по фильтру (crm.contact.list). Ключевые поля: NAME, LAST_NAME, PHONE, EMAIL, COMPANY_ID, DATE_CREATE.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {"type": "object", "description": _FILTER_DESC},
                    "select": {"type": "array", "items": {"type": "string"}, "description": _SELECT_CONTACT_DESC},
                    "limit": {"type": "integer", "description": _LIMIT_DESC},
                },
                "required": ["filter"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_companies",
            "description": "Поиск компаний в Bitrix24 по фильтру (crm.company.list). Ключевые поля: TITLE, PHONE, EMAIL. ИНН недоступен.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {"type": "object", "description": _FILTER_DESC},
                    "select": {"type": "array", "items": {"type": "string"}, "description": _SELECT_COMPANY_DESC},
                    "limit": {"type": "integer", "description": _LIMIT_DESC},
                },
                "required": ["filter"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_categories",
            "description": "Список категорий (воронок) сделок",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_stages",
            "description": "Список стадий сделок для категории",
            "parameters": {
                "type": "object",
                "properties": {
                    "category_id": {"type": "integer", "description": "ID категории воронки"},
                },
                "required": ["category_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_users",
            "description": "Список активных пользователей Bitrix24",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_entity_fields",
            "description": "Схема полей сущности (deal, contact, company)",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_type": {
                        "type": "string",
                        "enum": ["deal", "contact", "company"],
                    },
                },
                "required": ["entity_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_export",
            "description": "Запустить фоновую задачу выгрузки/импорта данных в Excel",
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["region", "stage", "category_full"],
                    },
                    "params": {
                        "type": "object",
                        "description": _EXPORT_PARAMS_DESC,
                    },
                },
                "required": ["mode", "params"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_lpr_tomoru",
            "description": (
                "Выбрать ЛПР (лиц, принимающих решения) по региону и подготовить файл для "
                "выгрузки в Tomoru: лист «Номера» (телефоны 7XXXXXXXXXX в столбик) и лист "
                "«Отчёт» (человекочитаемые данные). Выполняется сразу, возвращает превью и "
                "ссылку на скачивание."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "region_name": {
                        "type": "string",
                        "description": "Название региона, напр. «Томская область». Резолвится автоматически.",
                    },
                    "region_id": {
                        "type": "integer",
                        "description": "ID элемента списка регионов (если уже известен).",
                    },
                    "category_id": {
                        "type": "integer",
                        "description": "ID воронки сделок (по умолчанию 15).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": _LIMIT_DESC,
                    },
                },
            },
        },
    },
]


@dataclass
class AIChatResult:
    reply: str
    table_columns: list[str] = field(default_factory=list)
    table_rows: list[dict[str, Any]] = field(default_factory=list)
    download_url: str | None = None
    download_label: str | None = None


class AIService:
    def __init__(self, settings: Settings, db: Session):
        if not settings.openai_api_key:
            raise ValueError("OpenAI API ключ не настроен")
        self.settings = settings
        self.db = db
        self.client = OpenAI(api_key=settings.openai_api_key)
        self.bitrix: BitrixClient | None = None
        if settings.bitrix_webhook_url:
            self.bitrix = BitrixClient(settings)
        self._last_table: dict[str, Any] | None = None
        self._last_download: dict[str, str] | None = None

    def chat(self, messages: list[dict[str, str]]) -> AIChatResult:
        api_messages: list[dict[str, Any]] = [{"role": "system", "content": FULL_SYSTEM_PROMPT}]
        api_messages.extend(messages)
        self._last_table = None
        self._last_download = None

        for _ in range(MAX_TOOL_ITERATIONS):
            response = self.client.chat.completions.create(
                model=self.settings.openai_model,
                messages=api_messages,
                tools=TOOLS,
                tool_choice="auto",
            )
            choice = response.choices[0]
            assistant_msg = choice.message

            if assistant_msg.tool_calls:
                api_messages.append(self._assistant_to_dict(assistant_msg))
                for tool_call in assistant_msg.tool_calls:
                    fn_name = tool_call.function.name
                    try:
                        fn_args = json.loads(tool_call.function.arguments or "{}")
                    except json.JSONDecodeError:
                        fn_args = {}
                    try:
                        result = self._execute_tool(fn_name, fn_args)
                    except AppError as exc:
                        result = {"error": exc.user_message}
                    except Exception as exc:
                        logger.exception("Tool %s failed", fn_name)
                        result = {"error": str(exc)}
                    api_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps(result, ensure_ascii=False, default=str),
                        }
                    )
                continue

            reply = assistant_msg.content or ""
            table_columns: list[str] = []
            table_rows: list[dict[str, Any]] = []
            if self._last_table:
                table_columns = self._last_table.get("columns", [])
                table_rows = self._last_table.get("rows", [])
            download_url = self._last_download.get("url") if self._last_download else None
            download_label = self._last_download.get("label") if self._last_download else None
            return AIChatResult(
                reply=reply,
                table_columns=table_columns,
                table_rows=table_rows,
                download_url=download_url,
                download_label=download_label,
            )

        return AIChatResult(
            reply="Превышено максимальное число шагов обработки запроса. Попробуйте уточнить задачу.",
        )

    @staticmethod
    def _assistant_to_dict(message: Any) -> dict[str, Any]:
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": message.content,
        }
        if message.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in message.tool_calls
            ]
        return msg

    def _require_bitrix(self) -> BitrixClient:
        if not self.bitrix:
            raise BitrixAuthenticationError("URL вебхука Bitrix24 не настроен")
        return self.bitrix

    def _clamp_limit(self, limit: int | None) -> int:
        if limit is None or limit <= 0:
            return min(50, self.settings.max_export_size)
        return min(limit, self.settings.max_export_size)

    def _execute_tool(self, name: str, args: dict[str, Any]) -> Any:
        if name == "search_deals":
            return self._search_entity("deal", args)
        if name == "search_contacts":
            return self._search_entity("contact", args)
        if name == "search_companies":
            return self._search_entity("company", args)
        if name == "list_categories":
            return self._require_bitrix().get_categories()
        if name == "list_stages":
            return self._require_bitrix().get_stages(int(args["category_id"]))
        if name == "list_users":
            return self._require_bitrix().get_users()
        if name == "get_entity_fields":
            return self._require_bitrix().get_entity_fields(str(args["entity_type"]))
        if name == "start_export":
            return self._start_export(str(args["mode"]), args.get("params") or {})
        if name == "export_lpr_tomoru":
            return self._export_lpr_tomoru(args)
        raise ValueError(f"Неизвестный инструмент: {name}")

    def _search_entity(self, entity: str, args: dict[str, Any]) -> dict[str, Any]:
        client = self._require_bitrix()
        entity_filter = args.get("filter") or {}
        select = args.get("select") or list(DEFAULT_SELECT)
        limit = self._clamp_limit(args.get("limit"))

        method_map = {
            "deal": "crm.deal.list",
            "contact": "crm.contact.list",
            "company": "crm.company.list",
        }
        method = method_map[entity]
        rows = client.get_paginated(
            method,
            {"select": select, "filter": entity_filter, "order": {"ID": "ASC"}},
            limit=limit,
        )
        table = self._rows_to_table(rows, select)
        self._last_table = table
        return {
            "count": len(rows),
            "columns": table["columns"],
            "rows": table["rows"][:20],
            "truncated": len(rows) > 20,
        }

    @staticmethod
    def _rows_to_table(rows: list[dict[str, Any]], select: list[str]) -> dict[str, Any]:
        if not rows:
            return {"columns": select, "rows": []}
        columns: list[str] = []
        seen: set[str] = set()
        for field_code in select:
            if field_code not in seen:
                seen.add(field_code)
                columns.append(field_code)
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    columns.append(key)
        flat_rows: list[dict[str, Any]] = []
        for row in rows:
            flat: dict[str, Any] = {}
            for col in columns:
                val = row.get(col)
                if isinstance(val, (dict, list)):
                    flat[col] = json.dumps(val, ensure_ascii=False)
                else:
                    flat[col] = val
            flat_rows.append(flat)
        return {"columns": columns, "rows": flat_rows}

    def _resolve_region_id(self, args: dict[str, Any]) -> tuple[int, str]:
        region_id = args.get("region_id")
        region_name = (args.get("region_name") or "").strip()
        if region_id:
            return int(region_id), region_name
        if not region_name:
            raise ValueError("Не указан регион (region_name или region_id)")
        client = self._require_bitrix()
        matches = client.find_regions(region_name, DEFAULT_REGION_IBLOCK_ID)
        if not matches:
            raise ValueError(f"Регион «{region_name}» не найден в справочнике")
        chosen = matches[0]
        return int(chosen["id"]), chosen.get("name") or region_name

    def _export_lpr_tomoru(self, args: dict[str, Any]) -> dict[str, Any]:
        self._require_bitrix()
        region_id, region_name = self._resolve_region_id(args)
        category_id = int(args.get("category_id") or 15)
        limit = self._clamp_limit(args.get("limit"))

        lpr_config = load_lpr_config(self.db)
        params = {
            "region_id": region_id,
            "region_name": region_name,
            "category_id": category_id,
            "limit": limit,
        }

        service = LprTomoruService(
            settings=self.settings,
            cancel_check=lambda: False,
            lpr_config=lpr_config,
        )
        result_path = service.run_lpr_tomoru_export(params)

        job = self._record_completed_job("region_lpr", params, result_path, service.stats)

        preview_columns = [
            "Телефон",
            "ФИО",
            "Должность",
            "Компания",
            "Сделка",
            "Регион",
            "Признак ЛПР",
        ]
        preview_rows = [
            {
                "Телефон": row.phone,
                "ФИО": row.fio,
                "Должность": row.post,
                "Компания": row.company,
                "Сделка": f"{row.deal_id} — {row.deal_title}",
                "Регион": row.region,
                "Признак ЛПР": row.reason,
            }
            for row in service.report_rows
        ]
        self._last_table = {"columns": preview_columns, "rows": preview_rows}
        download_url = f"/exports/{job.id}/download"
        self._last_download = {
            "url": download_url,
            "label": "Скачать файл для Tomoru (.xlsx)",
        }

        return {
            "job_id": job.id,
            "region": region_name,
            "category_id": category_id,
            "lpr_phone_count": len(service.phones),
            "deals_total": service.stats.deals_total,
            "download_url": download_url,
            "preview_rows": preview_rows[:20],
            "preview_truncated": len(preview_rows) > 20,
            "message": (
                f"Подготовлен файл для Tomoru: найдено {len(service.phones)} номеров ЛПР. "
                f"Скачать: {download_url}"
            ),
        }

    def _record_completed_job(
        self,
        mode: str,
        params: dict[str, Any],
        result_path: str,
        stats: ExportStatistics,
    ) -> ExportJob:
        safe_params = {k: v for k, v in params.items() if "webhook" not in k.lower()}
        safe_params["_mode"] = mode
        now = utcnow()
        job = ExportJob(
            mode=mode,
            status="completed",
            parameters_json=json.dumps(safe_params, ensure_ascii=False),
            progress_current=stats.deals_total,
            progress_total=stats.deals_total,
            progress_percent=100.0,
            current_step="Завершено",
            result_file=result_path,
            started_at=now,
            finished_at=now,
            statistics_json=json.dumps(stats.to_dict(), ensure_ascii=False),
            event_log_json=json.dumps(
                [f"[{now.strftime('%H:%M:%S')}] Выгрузка ЛПР для Tomoru завершена"],
                ensure_ascii=False,
            ),
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    def _start_export(self, mode: str, params: dict[str, Any]) -> dict[str, Any]:
        allowed_modes = {"region", "stage", "category_full"}
        if mode not in allowed_modes:
            raise ValueError(f"Недопустимый режим: {mode}")
        if "limit" in params:
            params["limit"] = self._clamp_limit(int(params["limit"]))
        job = JobService().create_job(self.db, mode, params)
        return {
            "job_id": job.id,
            "status": job.status,
            "message": f"Задача выгрузки #{job.id} создана. Отслеживание: /exports/{job.id}",
        }
