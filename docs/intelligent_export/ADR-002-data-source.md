# ADR-002: Источник данных для intelligent export

**Статус:** Принято  
**Дата:** 2026-06-25  
**Контекст:** [`AIService`](../../app/services/ai_service.py) использует **live Bitrix REST**. [`ImportOrchestrator`](../../app/services/bitrix_import/orchestrator.py) наполняет **локальную PostgreSQL**.

---

## Решение

### Primary source: локальная PostgreSQL

Intelligent export **читает данные только из локальной БД** через `QueryCompiler` → `CrmEntity` (+ joins по plan).

**Обоснование:**
- Предсказуемая производительность и `COUNT` для preview.
- Единый audit trail (какой snapshot импорта использован).
- AI не получает произвольный доступ к Bitrix filters (см. ADR-004).
- Согласованность с каталогом полей (`crm_field_definitions`, semantics, dictionaries).

**Обязательное условие:** актуальный CRM import (`sync_runs` completed, checkpoint fresh). UI показывает `last_successful_sync_at` из `sync_checkpoints`.

### Secondary source: live Bitrix — только служебно

| Сценарий | Live Bitrix | Local DB |
|----------|-------------|----------|
| Intelligent export (preview/run) | **Нет** | **Да** |
| Legacy AI chat ([`ai.py`](../../app/routers/ai.py)) | Да (сохранить) | Нет |
| Legacy exports (region/stage) | Да | Нет |
| Staleness warning | `profile` / dashboard timestamp | checkpoint age |
| Strict scope verify (фаза 2) | COUNT only | primary |

### Entity coverage

| entity_type_id | Импорт | Intelligent export MVP |
|----------------|--------|------------------------|
| 1 Lead | Да | **Да** (включить в catalog) |
| 2 Deal | Да | Да |
| 3 Contact | Да | Да |
| 4 Company | Да | Да |

Leads ранее отсутствовали в AI tools — в intelligent export **включаются** через единый `CrmEntity` model.

### Связи между сущностями

**MVP:** join по ID из `raw_payload` (`contactId`, `companyId`, `contactIds`) + denorm columns (`category_id`, `stage_id`).

**Фаза 2:** normalized `crm_entity_field_values` (ADR-003) для фильтров по UF без JSONB path.

### Freshness policy

| Checkpoint age | Поведение |
|----------------|-----------|
| < 1 час | normal |
| 1–24 часа | warning banner |
| > 24 часа | block run (allow preview with warning); suggest incremental import |

Константы — `app_settings` keys: `ie_max_staleness_hours`.

---

## Последствия

- Query compiler **не зависит** от `BitrixClient`.
- Пользователь должен понимать lag импорта.
- Import worker становится **критической** зависимостью intelligent export.

## Альтернативы (отклонены)

| Альтернатива | Причина |
|--------------|---------|
| Live Bitrix primary | AI/tools риск; rate limits; нет единого plan validation |
| Hybrid per-request live fetch | Непредсказуемое время; сложный audit |
| Dual-write on export | Дублирование логики legacy + intelligent |
