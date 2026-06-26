# Bitrix24 CRM Import Module

## Архитектура

```
UI (Jinja2) ──► Admin API (/admin/bitrix/*)
                      │
                      ▼
               sync_runs (PostgreSQL)
                      │
                      ▼
               Worker (app/worker.py)
                      │
         ┌────────────┼────────────┐
         ▼            ▼            ▼
  BitrixCrmClient  CrmRepository  MetadataAI
  (crm.item.*)     (upsert)       (OpenAI)
```

### Компоненты

| Компонент | Файл | Назначение |
|-----------|------|------------|
| Bitrix API client | `app/services/bitrix_import/bitrix_crm_client.py` | crm.item.*, справочники, дочерние данные |
| Import orchestrator | `app/services/bitrix_import/orchestrator.py` | full/incremental/reconciliation |
| Discovery | `app/services/bitrix_import/discovery_service.py` | поля, справочники, UF_CRM_* |
| AI analyzer | `app/services/bitrix_import/metadata_ai_service.py` | обезличенный AI-анализ метаданных |
| Worker | `app/worker.py` | DB-backed job runner |
| Repositories | `app/repositories/` | persistence, upsert, версии |
| Admin API | `app/routers/admin_bitrix.py` | REST + UI endpoints |
| File storage | `app/services/bitrix_import/file_storage.py` | локальный volume / S3-ready |

## Схема импорта

### Full import (первый запуск)
1. sync_run → schema → dictionaries → leads/deals → related contacts/companies
2. overlap re-read → AI analysis → checkpoints

### Incremental import (кнопка по умолчанию)
- Курсор: `updatedTime ASC, id ASC` с overlap 10 минут
- Upsert + `payload_hash` для идемпотентности

### Reconciliation
- Сверка ID с Bitrix24
- Soft delete (`is_deleted=true`) при отсутствии в Bitrix
- Guard: не помечать удалёнными при проблемах доступа

## Переменные окружения

```env
DATABASE_URL=postgresql+psycopg://bitrix:change-me@db:5432/bitrix_export
POSTGRES_USER=bitrix
POSTGRES_PASSWORD=change-me
POSTGRES_DB=bitrix_export
FILE_STORAGE_DIR=./filestorage
OPENAI_BITRIX_METADATA_MODEL=   # fallback: OPENAI_MODEL
BITRIX_METADATA_PROMPT_VERSION=1
WORKER_POLL_INTERVAL=2.0
WORKER_HEARTBEAT_INTERVAL=30.0
WORKER_STALE_RUN_MINUTES=15
IMPORT_BATCH_SIZE=50
IMPORT_OVERLAP_MINUTES=10
```

## Команды

Все команды выполняются из каталога `bitrix_export_web` при запущенном стеке (`docker compose up --build`), если не указано иное.

### Запуск стека
```bash
cd bitrix_export_web
docker compose up --build
```

### Миграции
```bash
docker compose run --rm migrate
```

### Тесты
```bash
docker compose exec web pytest
```

### Очистка CRM-данных перед повторным импортом
```bash
docker compose exec web python scripts/clear_crm_import_data.py
```
Очищает CRM-таблицы и `sync_runs` / `sync_checkpoints`. Не трогает `app_settings`, `export_jobs`, `ai_prompt_templates`.

## Таблицы

- `sync_runs`, `sync_checkpoints`
- `crm_entities`, `crm_entity_versions`
- `crm_field_definitions`, `crm_field_definition_versions`, `crm_field_semantics`
- `crm_dictionaries`, `crm_dictionary_entries`
- `crm_entity_field_values`
- `crm_child_records`, `crm_files`, `crm_users`, `crm_currencies`
- Legacy: `app_settings`, `ai_prompt_templates`, `export_jobs`

## Bitrix24 методы

- `crm.item.fields`, `crm.item.list`, `crm.item.get` (useOriginalUfNames=Y)
- `crm.status.list`, `crm.category.list`, `crm.currency.list`
- `user.get`, `crm.item.productrow.list`
- `crm.activity.list`, `crm.timeline.comment.list`, `crm.stagehistory.list`
- `crm.requisite.list`, `crm.address.list`, `crm.requisite.bankdetail.list`
- `disk.file.get` (файлы)

## Ограничения webhook

Входящий webhook **не поддерживает** подписку на события Bitrix24.
Используется polling по `updatedTime` + периодическая reconciliation.

## OpenAI / обезличивание

В OpenAI отправляются **только**:
- метаданные полей (названия, типы, settings)
- обезличенная статистика и примеры ([EMAIL], [PHONE], [PERSON] и т.д.)

**Не отправляются**: карточки CRM, телефоны, email, комментарии, файлы, токены.

Повторный AI-анализ блокируется через `source_hash` (metadata + stats + prompt_version).

## Восстановление после ошибки

- Worker восстанавливает зависшие runs (`heartbeat_at` > 15 мин → failed)
- Повторный запуск безопасен (upsert + payload_hash)
- Checkpoints обновляются только после успешного commit пакета

## Резервное копирование

```bash
# PostgreSQL
docker compose exec db pg_dump -U bitrix bitrix_export > backup.sql

# Файлы
tar -czf filestorage_backup.tar.gz filestorage/
```
