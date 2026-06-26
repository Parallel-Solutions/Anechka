# Спецификация API Bitrix24 Export

Веб-приложение для выгрузки сделок Bitrix24 в Excel. Документ описывает все HTTP-ручки FastAPI, форматы запросов и ответов, жизненный цикл задач и внутренние вызовы Bitrix24 REST API.

**Источники в коде:** `app/routers/`, `app/schemas.py`, `app/services/bitrix_client.py`.

Интерактивная документация (OpenAPI): **http://localhost:8000/docs**

Сервер доступен только при запущенном `docker compose up`.

---

## 1. Общие сведения

| Параметр | Значение |
|----------|----------|
| Base URL | `http://localhost:8000` |
| OpenAPI | `/docs`, `/openapi.json` |
| Аутентификация | HTTP Basic Auth (если задан `BASIC_AUTH_PASSWORD` в `.env`) |
| Формат JSON | `Content-Type: application/json` |
| Предусловие | Настроен `bitrix_webhook_url` (страница `/settings` или env `BITRIX_WEBHOOK_URL`) |

**Приоритет настроек:** сохранённые в SQLite (UI) → переменные `.env` → значения по умолчанию (`app/config.py`).

**Ключевые настройки по умолчанию:**

| Параметр | Default | Описание |
|----------|---------|----------|
| `max_export_size` | 5000 | Максимум сделок в одной выгрузке |
| `connect_timeout` | 10 с | Таймаут подключения к Bitrix |
| `read_timeout` | 60 с | Таймаут чтения ответа Bitrix |
| `max_retries` | 5 | Повторы при ошибках / rate limit |
| `retry_base_delay` | 1.0 с | Базовая задержка между повторами |

---

## 2. Сводная таблица HTTP-маршрутов

| Метод | URL | Тип | Назначение |
|-------|-----|-----|------------|
| GET | `/` | HTML | Главная: форма выгрузки |
| GET | `/settings` | HTML | Страница настроек |
| GET | `/exports` | HTML | Список задач |
| GET | `/exports/{job_id}` | HTML | Детали задачи |
| GET | `/api/categories` | JSON | Воронки сделок |
| GET | `/api/categories/{category_id}/stages` | JSON | Стадии воронки |
| GET | `/api/users` | JSON | Активные пользователи |
| GET | `/api/regions/search` | JSON | Поиск региона |
| POST | `/exports/region` | JSON | Запуск выгрузки по региону |
| POST | `/exports/stage` | JSON | Запуск выгрузки по стадии |
| POST | `/exports/category-full` | JSON | Полная выгрузка по воронке |
| GET | `/exports` | JSON | Список задач (API) |
| GET | `/exports/{job_id}` | JSON | Статус задачи (API) |
| GET | `/api/exports/{job_id}/status` | JSON / SSE | Статус с polling или SSE |
| POST | `/api/exports/{job_id}/cancel` | JSON | Отмена задачи |
| POST | `/api/exports/{job_id}/retry` | JSON | Перезапуск задачи |
| GET | `/exports/{job_id}/download` | File | Скачать XLSX |
| GET | `/exports/{job_id}/download/json` | File | Скачать JSON |
| POST | `/settings` | Form | Сохранение настроек |
| POST | `/api/connection/test` | JSON | Проверка вебхука |

Статические файлы: `/static/*`

---

## 3. Bitrix-справочники (`/api/*`)

Роутер: `app/routers/bitrix.py` (префикс `/api`).

Для всех ручек этого раздела требуется настроенный вебхук. Если URL не задан — **400** с `{"detail": "URL вебхука не настроен"}`.

### `GET /api/categories`

Список воронок сделок. Всегда включает виртуальную категорию «Общая» с `id=0`.

**Bitrix:** `crm.category.list` (`entityTypeId: 2`)

**Ответ 200:** массив `CategoryItem`

```json
[
  {"id": 0, "name": "Общая"},
  {"id": 15, "name": "Продажи"}
]
```

**Ошибки:** `502` — ошибка Bitrix24 (`detail`: пользовательское сообщение из `AppError`)

---

### `GET /api/categories/{category_id}/stages`

Стадии выбранной воронки.

**Path-параметры:**

| Имя | Тип | Описание |
|-----|-----|----------|
| `category_id` | int | ID воронки (`0` — общая воронка) |

**Bitrix:** `crm.status.list` с `filter.ENTITY_ID`:
- `category_id=0` → `DEAL_STAGE`
- иначе → `DEAL_STAGE_{category_id}`

**Ответ 200:** массив `StageItem`

```json
[
  {"id": "C15:NEW", "name": "Новая", "category_id": 15}
]
```

---

### `GET /api/users`

Активные пользователи портала. Пагинация выполняется внутри `BitrixClient.get_paginated`.

**Bitrix:** `user.get` (`ACTIVE: true`)

**Ответ 200:** массив `UserItem`, отсортированный по имени

```json
[
  {"id": 439, "name": "Иванов Иван"}
]
```

---

### `GET /api/regions/search`

Поиск региона по названию в инфоблоке списков.

**Query-параметры:**

| Имя | Тип | Обяз. | Default | Описание |
|-----|-----|-------|---------|----------|
| `name` | string | да | — | Название региона (min length 1) |
| `iblock_id` | int | нет | 49 | ID инфоблока |

**Bitrix:** `lists.element.get`

```json
{
  "IBLOCK_TYPE_ID": "lists",
  "IBLOCK_ID": 49,
  "FILTER": {"NAME": "<name>"}
}
```

**Ответ 200:** массив `RegionSearchResult`

```json
[
  {"id": 12345, "name": "Томская область"}
]
```

**Ошибки:**
- `404` — `{"detail": "Указанный регион не найден"}`
- `502` — ошибка Bitrix24

---

## 4. Выгрузки

Роутер: `app/routers/exports.py`. Задачи выполняются в фоне (`JobService`, ThreadPoolExecutor).

### `POST /exports/region`

Создаёт фоновую задачу выгрузки телефонов по региону (формат `tel_po_reg`).

**Body:** `RegionExportRequest`

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `region_name` | string | — | Название региона (обяз.) |
| `region_id` | int \| null | null | ID элемента инфоблока |
| `category_id` | int | 15 | Воронка сделок |
| `iblock_id` | int | 49 | Инфоблок регионов (для UI-поиска) |
| `region_field` | string | `UF_CRM_5ECE25C5D78E0` | UF-поле региона в сделке |
| `limit` | int | 500 | Макс. сделок (> 0, ≤ `max_export_size`) |

**Пример запроса:**

```json
{
  "region_name": "Томская область",
  "region_id": 100,
  "category_id": 15,
  "iblock_id": 49,
  "limit": 500
}
```

**Ответ 200:** `MessageResponse`

```json
{
  "message": "Задача создана",
  "job_id": 42
}
```

**Ошибки:**
- `400` — `{"detail": "Не указан ID региона"}`
- `400` — `{"detail": "Лимит не может превышать <max_export_size>"}`

**Фоновая логика:** `TelPoRegService.run_region_phones_export`
- Фильтр сделок: `CATEGORY_ID`, `{region_field}: region_id`
- Select: `ID`, `TITLE`, `UF_CRM_5ECE25C5D78E0`, `CATEGORY_ID`
- Результат: XLSX, лист «Сделки», одна строка на сделку

---

### `POST /exports/stage`

Создаёт фоновую задачу выгрузки по стадии воронки.

**Body:** `StageExportRequest`

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `category_id` | int | — | ID воронки (обяз.) |
| `stage_id` | string | — | ID стадии, напр. `C15:NEW` (обяз.) |
| `limit` | int | 50 | Макс. сделок |
| `excluded_user_ids` | int[] | `[]` | Исключить ответственных (`!ASSIGNED_BY_ID`) |
| `excel_format` | `"normalized"` \| `"wide"` | `"normalized"` | Формат Excel |
| `include_company_phones` | bool | true | Телефоны компании |
| `include_company_contacts` | bool | true | Контакты компании |
| `all_contact_phones` | bool | true | Все телефоны контакта (иначе первый) |
| `region_field` | string | `UF_CRM_5ECE25C5D78E0` | UF-поле региона |

**Пример запроса:**

```json
{
  "category_id": 15,
  "stage_id": "C15:NEW",
  "limit": 100,
  "excluded_user_ids": [1, 2],
  "excel_format": "normalized"
}
```

**Валидация:** если вебхук настроен, `stage_id` должен принадлежать `category_id` — иначе **400**:

```json
{"detail": "Стадия не принадлежит выбранной категории"}
```

**Фоновая логика:** `ExportService.run_stage_export`
- Фильтр: `CATEGORY_ID`, `STAGE_ID`, опционально `!ASSIGNED_BY_ID`
- Форматы Excel: normalized (строка на телефон) или wide (строка на сделку)

---

### `POST /exports/category-full`

Создаёт фоновую задачу **полной выгрузки** всех сделок выбранной воронки с максимально полными данными из Bitrix24.

**Body:** `CategoryFullExportRequest`

| Поле | Тип | Default | Описание |
|------|-----|---------|----------|
| `category_id` | int | — | ID воронки (обяз.) |
| `limit` | int | 5000 | Макс. сделок (≤ `max_export_size`) |
| `excluded_user_ids` | int[] | `[]` | Исключить ответственных (`!ASSIGNED_BY_ID`) |

**Пример запроса:**

```json
{
  "category_id": 15,
  "limit": 5000,
  "excluded_user_ids": [1, 2]
}
```

**Валидация:**
- `limit > max_export_size` → **400**
- если вебхук настроен, `category_id` должен существовать → иначе **400** `{"detail": "Категория не найдена"}`

**Фоновая логика:** `FullCategoryExportService.run_category_full_export`
- Фильтр: `CATEGORY_ID`, опционально `!ASSIGNED_BY_ID`
- Bitrix: `crm.deal.fields`, `crm.deal.list` (ID), batch `crm.deal.get`, batch `crm.deal.contact.items.get`, batch `crm.contact.get`, batch `crm.company.get`
- Результат: XLSX с листами **Deals**, **Contacts**, **Companies**, **DealContacts**, **Информация**
- Сложные поля (multifield, массивы) — JSON в ячейке; значения > 32 767 символов помечаются `[TRUNCATED_BY_EXCEL_LIMIT]`, полный текст — в companion-файле `.overflow.json`

**Дополнительные колонки на листе Deals:** `_category_name`, `_stage_name`, `_assigned_name`

---

### `GET /exports`

Список всех задач выгрузки, от новых к старым.

**Ответ 200:** массив `ExportJobResponse`

---

### `GET /exports/{job_id}`

Статус одной задачи.

**Path:** `job_id` (int)

**Ответ 200:** `ExportJobResponse`

**Ошибки:** `404` — `{"detail": "Задача не найдена"}`

---

### `GET /api/exports/{job_id}/status`

Статус задачи с поддержкой Server-Sent Events.

**Режим JSON** — заголовок `Accept: application/json`:

- **Ответ 200:** `ExportJobResponse`

**Режим SSE** — заголовок `Accept: text/event-stream`:

- **Content-Type:** `text/event-stream`
- Каждые ~1.5 с отправляется событие: `data: {ExportJobResponse JSON}\n\n`
- Поток завершается при статусе `completed`, `failed` или `cancelled`
- Если задача не найдена: одно событие `data: {"error": "not found"}\n\n`

---

### `POST /api/exports/{job_id}/cancel`

Запрос отмены выполняющейся задачи.

**Body:** `{}` (пустой JSON)

**Ответ 200:** `ExportJobResponse` с `cancel_requested: true`

**Ошибки:** `404` — задача не найдена

Отмена кооперативная: worker проверяет флаг между итерациями.

---

### `POST /api/exports/{job_id}/retry`

Создаёт новую задачу с теми же параметрами, что у указанной.

**Body:** `{}`

**Ответ 200:** `MessageResponse`

```json
{
  "message": "Задача перезапущена",
  "job_id": 43
}
```

**Ошибки:** `404` — исходная задача не найдена

---

### `GET /exports/{job_id}/download`

Скачивание готового XLSX.

**Условия:** `status=completed`, `result_file` задан, файл внутри `export_dir`

**Ответ 200:**
- **Content-Type:** `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`
- **Content-Disposition:** имя файла из пути

**Ошибки:**
- `404` — `{"detail": "Файл недоступен"}`
- `403` — `{"detail": "..."}` (path traversal)

---

### `GET /exports/{job_id}/download/json`

Скачивание JSON с полными данными выгрузки (создаётся рядом с XLSX при завершении задачи).

**Условия:** `status=completed`, `result_file` задан, `{stem}.json` существует и находится внутри `export_dir`

**Ответ 200:**
- **Content-Type:** `application/json`
- **Content-Disposition:** имя файла из пути

**Ошибки:**
- `404` — `{"detail": "Файл недоступен"}` или `{"detail": "JSON-файл недоступен"}`
- `403` — path traversal

---

### Модель `ExportJobResponse`

| Поле | Тип | Описание |
|------|-----|----------|
| `id` | int | ID задачи |
| `mode` | string | `"region"`, `"stage"` или `"category_full"` |
| `status` | string | См. жизненный цикл |
| `progress_current` | int | Текущий прогресс |
| `progress_total` | int | Всего элементов |
| `progress_percent` | float | Процент (0–100) |
| `current_step` | string | Текущий шаг (текст) |
| `result_file` | string \| null | Абсолютный путь к XLSX |
| `created_at` | datetime (ISO 8601) | Время создания |
| `started_at` | datetime \| null | Время старта |
| `finished_at` | datetime \| null | Время завершения |
| `error_message` | string \| null | Сообщение об ошибке |
| `cancel_requested` | bool | Запрошена отмена |
| `statistics` | object | Статистика выгрузки |
| `event_log` | string[] | Лог событий (до 100 записей) |
| `parameters` | object | Параметры запуска (включая `_mode`) |

**Объект `statistics`:**

| Ключ | Тип | Описание |
|------|-----|----------|
| `deals_total` | int | Всего сделок |
| `deals_processed` | int | Обработано |
| `contacts_found` | int | Найдено контактов |
| `phones_found` | int | Найдено телефонов |
| `errors` | int | Ошибок при обработке |
| `skipped` | int | Пропущено |

**Пример:**

```json
{
  "id": 1,
  "mode": "region",
  "status": "completed",
  "progress_current": 10,
  "progress_total": 10,
  "progress_percent": 100.0,
  "current_step": "Завершено",
  "result_file": "C:\\random_forest\\simpleAnechka\\bitrix_export_web\\exports\\region_tomsk_20260623.xlsx",
  "created_at": "2026-06-23T10:00:00+00:00",
  "started_at": "2026-06-23T10:00:01+00:00",
  "finished_at": "2026-06-23T10:00:30+00:00",
  "error_message": null,
  "cancel_requested": false,
  "statistics": {
    "deals_total": 10,
    "deals_processed": 10,
    "contacts_found": 25,
    "phones_found": 30,
    "errors": 0,
    "skipped": 0
  },
  "event_log": [
    "[10:00:01] Задача запущена",
    "[10:00:30] Выгрузка завершена успешно"
  ],
  "parameters": {
    "region_name": "Томская область",
    "region_id": 100,
    "category_id": 15,
    "limit": 500,
    "_mode": "region"
  }
}
```

---

## 5. Настройки

Роутер: `app/routers/settings.py`

### `POST /settings`

Сохранение настроек через HTML-форму (не JSON API).

**Content-Type:** `application/x-www-form-urlencoded`

**Поля формы:**

| Поле | Тип | Default |
|------|-----|---------|
| `bitrix_webhook_url` | string | — |
| `connect_timeout` | float | 10.0 |
| `read_timeout` | float | 60.0 |
| `max_retries` | int | 5 |
| `retry_base_delay` | float | 1.0 |
| `max_export_size` | int | 5000 |
| `export_dir` | string | `./exports` |
| `log_level` | string | `INFO` |

**Поведение вебхука:**
- Пустое значение или маскированный URL (`/rest/***/***`) — текущий URL не перезаписывается
- Валидация URL: должен начинаться с `http://` или `https://`

**Ответ:** `303 See Other` → `/settings?saved=1&webhook=updated|unchanged`

---

### `POST /api/connection/test`

Проверка подключения к Bitrix24.

**Body:** `{}`

**Bitrix:** `profile`

**Ответ 200:** `ConnectionTestResponse`

```json
{"ok": true, "message": "Подключение к Bitrix24 успешно"}
```

```json
{"ok": false, "message": "URL вебхука не настроен"}
```

```json
{"ok": false, "message": "Не удалось подключиться к Bitrix24. Проверьте вебхук"}
```

---

## 6. HTML-страницы

Роутер: `app/routers/pages.py`. Возвращают HTML (Jinja2), не JSON.

| GET | Шаблон | Назначение |
|-----|--------|------------|
| `/` | `index.html` | Форма выгрузки, последние 20 задач, индикатор Bitrix |
| `/settings` | `settings.html` | Настройки приложения |
| `/exports` | `exports.html` | Полный список задач |
| `/exports/{job_id}` | `export_detail.html` | Детали задачи, лог, статистика |

---

## 7. Жизненный цикл задачи

```
queued → running → completed
                 → failed
                 → cancelled
```

| Статус | Описание |
|--------|----------|
| `queued` | Задача создана, ожидает worker |
| `running` | Выполняется выгрузка |
| `completed` | XLSX сохранён, доступен download |
| `failed` | Ошибка (`error_message` заполнен) |
| `cancelled` | Отменена пользователем |

**Retry:** `POST /api/exports/{job_id}/retry` создаёт новую задачу в `queued`.

**Перезапуск сервера:** задачи в `running` помечаются `failed` с сообщением «Выполнение было прервано перезапуском приложения» (`JobService.recover_interrupted_jobs`).

---

## 8. Коды HTTP-ошибок

| Код | Когда |
|-----|-------|
| 400 | Валидация: нет вебхука, нет `region_id`, превышен лимит, стадия не в категории, невалидный JSON body |
| 403 | Скачивание файла вне `export_dir` |
| 404 | Задача, файл или регион не найден |
| 502 | Ошибка Bitrix24 в прокси-ручках `/api/categories`, `/users`, … |

**Формат ошибки FastAPI:**

```json
{"detail": "Текст ошибки"}
```

или для validation errors — массив объектов с `loc`, `msg`, `type`.

---

## 9. Внутренние методы Bitrix24 REST

Не exposed как HTTP. Вызываются через `BitrixClient.call()`:

```
POST {bitrix_webhook_url}/{method}
Content-Type: application/json
```

| Метод Bitrix | Назначение | Пагинация |
|--------------|------------|-----------|
| `profile` | Проверка подключения | — |
| `crm.category.list` | Воронки (`entityTypeId: 2`) | — |
| `crm.status.list` | Стадии сделок | `get_paginated` |
| `user.get` | Пользователи | `get_paginated` |
| `lists.element.get` | Элементы списка (регионы) | — |
| `crm.deal.list` | Сделки по фильтру | `get_paginated` + `limit` |
| `crm.deal.fields` | Схема полей сделки | — |
| `crm.contact.fields` | Схема полей контакта | — |
| `crm.company.fields` | Схема полей компании | — |
| `crm.deal.get` | Одна сделка | batch в full export |
| `crm.deal.contact.items.get` | Контакты сделки | — |
| `crm.contact.get` | Один контакт | — |
| `crm.contact.list` | Контакты компании (fallback) | `get_paginated` |
| `crm.company.get` | Компания | — |
| `crm.company.contact.items.get` | Контакты компании | — |
| `batch` | Пакетные запросы (prefetch) | chunks по 50 |

### Пагинация Bitrix

Реализована в `BitrixClient.get_paginated()`:
- Запросы с параметром `start` (начиная с 0)
- Ответ содержит `result` (массив) и опционально `next` (следующий `start`)
- Цикл до отсутствия `next` или пустого `result`
- Параметр `limit` обрезает итоговый список

### Retry и rate limit

Повтор при HTTP 429, 500, 502, 503, 504 и ошибках Bitrix `QUERY_LIMIT_EXCEEDED`, `OVERLOAD_LIMIT`.
Экспоненциальная задержка: `retry_base_delay * 2^attempt`, до `max_retries` попыток.

---

## 10. Legacy-скрипты (вне FastAPI)

В корне workspace — справочные скрипты с прямыми POST к вебхуку. Логика частично перенесена в веб-приложение.

### `tel_po_reg.py`

| Функция | Bitrix-метод | Примечание |
|---------|--------------|------------|
| `get_region_id_by_name` | `lists.element.get` | Инфоблок 49 |
| `get_deals_by_region_id` | `crm.deal.list` | Ручная пагинация по `start` |
| `get_all_deal_contact_ids` | `crm.deal.get`, `crm.deal.contact.items.get`, `crm.company.get`, `crm.company.contact.items.get` | — |
| `get_contact_info` | `crm.contact.get` | Первый телефон |

**Отличия от веб-приложения:** хардкод `WEBHOOK_URL`, лимит по умолчанию 50, нет фоновых задач.

### `tel_po_stadii.py`

| Функция | Bitrix-метод | Примечание |
|---------|--------------|------------|
| `get_employee_name_by_id` | `user.get` | — |
| `get_deal_stage_name` | `crm.dealcategory.stage.list` | В веб-приложении: `crm.status.list` |
| `get_deals_by_stage` | `crm.dealcategory.list`, `crm.dealcategory.stage.list`, `crm.deal.list` | Поиск стадии по имени, исключение сотрудников по ФИО |

**Отличия:** исключение по именам (не ID), другие методы категорий (`crm.dealcategory.*` vs `crm.category.list`).

---

## 11. Форматы Excel (результат выгрузки)

### По региону (`mode=region`)

Лист **Сделки**. Одна строка — одна сделка. Столбцы: `ID Сделки`, `Название сделки`, пары `ФИО контакта N` / `Телефон контакта N`.

### По стадии — normalized

Одна строка — один телефон. Столбцы: ID сделки, название, категория, стадия, ответственный, компания, контакт, телефоны, источник, регион, дата.

### По стадии — wide

Одна строка — одна сделка. Столбцы: сотрудник, ID, название, регион, пары ФИО/телефон.

### Полная выгрузка по воронке (`mode=category_full`)

Листы **Deals**, **Contacts**, **Companies**, **DealContacts**, **Информация**. Все поля сущностей из Bitrix; multifield — JSON в ячейке. При превышении лимита Excel (32 767 символов) создаётся companion `.overflow.json`.

На листе **Информация** — параметры запуска и статистика (stage export и category_full).
