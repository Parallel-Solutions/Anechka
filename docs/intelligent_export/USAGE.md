# Intelligent Export — эксплуатация, API, безопасность

Документ описывает реализованную подсистему интеллектуальных выгрузок:
переменные окружения, миграции, роли, API и меры безопасности.

## 1. Переменные окружения

| Переменная | По умолчанию | Назначение |
|------------|--------------|------------|
| `APP_SECRET_KEY` | — | Ключ подписи session-cookie (обязателен в проде) |
| `BOOTSTRAP_ADMIN_EMAIL` | — | Email создаваемого при старте администратора |
| `BOOTSTRAP_ADMIN_PASSWORD` | — | Пароль bootstrap-администратора |
| `SESSION_COOKIE_NAME` | `ie_session` | Имя cookie сессии |
| `SESSION_MAX_AGE_SECONDS` | `604800` | Время жизни сессии |
| `COOKIE_SECURE` | `false` | `Secure`-флаг cookie (включать в проде за HTTPS) |
| `IE_DEFAULT_PORTAL_ID` | `default` | Портал по умолчанию для скоупинга |
| `IE_PREVIEW_ROWS` | `100` | Лимит строк предпросмотра |
| `IE_MAX_EXPORT_ROWS` | `100000` | Жёсткий лимит строк выгрузки |
| `IE_STALENESS_WARN_HOURS` | `24` | Порог предупреждения об устаревании данных |
| `IE_STALENESS_BLOCK_HOURS` | `72` | Порог блокировки выгрузки |
| `IE_STATEMENT_TIMEOUT_MS` | `30000` | Таймаут SQL-запросов компилятора |
| `IE_MAX_MESSAGE_CHARS` | `8000` | Лимит длины сообщения чата |
| `IE_MAX_HISTORY_MESSAGES` | `20` | Сколько сообщений истории передаётся планировщику |
| `IE_RUN_RETENTION_DAYS` | `90` | Срок хранения записей о запусках |
| `IE_AUDIT_RETENTION_DAYS` | `365` | Срок хранения аудит-лога |

## 2. Миграции

Цепочка Alembic:

```
2b32241187b3  → 3a1c9e2f4b01  → 4b2d7c1a9f02  → 5c3e8d4a2b13
(базовая)        (export_plan)   (memory workflow) (audit log)
```

Применение: `docker compose run --rm migrate`. Smoke-тест миграций — `tests/test_migrations.py`
(чистая SQLite, upgrade head → downgrade до базовой ревизии).

## 3. Роли (RBAC)

| Роль | Возможности |
|------|-------------|
| `viewer` | Просмотр своих диалогов/планов/запусков; данные ограничены `ASSIGNED_BY_ID`; запрет на чувствительные поля |
| `analyst` | Всё, что viewer + создание диалогов, чат, сохранение планов, предпросмотр и запуск выгрузок |
| `admin` | Всё, что analyst + утверждение/отклонение project-памяти, доступ к `GET /audit` |

Скоупинг enforced на уровне репозитория (`ScopeContext`: `portal_id` + владелец) и
компилятора (`portal_id`, исключение `is_deleted`, для viewer — `assigned_by_id`).

## 4. API (`/api/intelligent-export`)

### Auth
- `POST /auth/login` — вход (email/password) → устанавливает session-cookie
- `POST /auth/logout` — выход
- `GET  /auth/me` — текущий пользователь

### Диалоги
- `POST   /conversations` — создать (analyst+)
- `GET    /conversations` — список своих
- `GET    /conversations/{id}` — получить
- `PATCH  /conversations/{id}` — переименовать
- `DELETE /conversations/{id}` — удалить
- `GET    /conversations/{id}/messages` — сообщения
- `POST   /conversations/{id}/chat` — реплика пользователю → ответ планировщика (analyst+)
- `POST   /conversations/{id}/plan` — сохранить план как новую версию
- `GET    /conversations/{id}/plans` — версии планов диалога

### Планы
- `GET  /plans/{id}` — версия плана
- `POST /plans/{id}/activate` — пометить активной
- `POST /plans/{id}/clone` — клонировать
- `POST /plans/{id}/count` — подсчёт строк
- `POST /plans/{id}/preview` — предпросмотр (с предупреждениями/staleness)
- `POST /plans/{id}/run` — запустить выгрузку (analyst+)

### Запуски
- `GET  /runs` — список своих запусков
- `GET  /runs/{id}` — статус запуска
- `POST /runs/{id}/cancel` — отмена
- `POST /runs/{id}/retry` — повтор
- `GET  /runs/{id}/download` — скачать результат (owner-guarded)

### Память
- `GET    /memory` — доступная память (system/project/user/dialog)
- `POST   /memory/proposals` — предложить запись
- `POST   /memory/{id}/approve` — утвердить (admin для project)
- `POST   /memory/{id}/reject` — отклонить
- `PATCH  /memory/{id}` — обновить
- `DELETE /memory/{id}` — soft-delete

### Аудит
- `GET /audit` — последние записи аудита (только admin)

## 5. Безопасность

- **AI никогда не порождает SQL/JSONPath/коды полей.** Любой ответ планировщика
  проходит: JSON parse → Pydantic → структурный validator → catalog validator →
  scope validator. Несуществующие/запрещённые поля отклоняются (см.
  `tests/test_security.py::test_plan_with_invented_field_is_rejected`).
- **Excel/CSV formula injection** — все значения проходят `sanitize_excel_value`
  (префикс `'` для `= + - @`).
- **Path traversal** — скачивание через `validate_download_path` (только внутри
  каталога экспорта).
- **IDOR** — все запросы скоупятся по владельцу и `portal_id`; чужой запуск/диалог →
  403/404.
- **Сессии** — подписанные cookie (`itsdangerous`), `HttpOnly`, `SameSite`,
  `Secure` в проде; пароли — `bcrypt`.
- **Лимиты** — длина сообщения (`IE_MAX_MESSAGE_CHARS`), строки выгрузки
  (`IE_MAX_EXPORT_ROWS`), таймаут SQL (`IE_STATEMENT_TIMEOUT_MS`).
- **Regex abuse** — длина паттерна ограничена, катастрофические конструкции
  (`(.*)+` и пр.) отклоняются на этапе валидации правил.
- **Audit log** — login, сохранение плана, запуск, скачивание, утверждение памяти
  пишутся в `ie_audit_log`. Ретеншн запусков/аудита — конфигурируемый.

## 6. Статус ADR

| ID | Тема | Статус |
|----|------|--------|
| ADR-001 | Auth, RBAC, права на данные | Реализовано |
| ADR-002 | Источник данных = локальная PostgreSQL | Реализовано |
| ADR-003 | Нормализация импорта (`crm_entity_field_values`) | Частично (MVP через `raw_payload` JSONB whitelist) |
| ADR-004 | ExportPlan 2.0 (datasets/sheets/typed params) | Реализовано |
| ADR-005 | Персистентность: диалоги, версии планов, память | Реализовано |
| ADR-006 | Job pipeline: preview + long-running export | Реализовано |
