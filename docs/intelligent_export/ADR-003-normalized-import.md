# ADR-003: Нормализация импорта (`crm_entity_field_values`, child records)

**Статус:** Принято  
**Дата:** 2026-06-25  
**Контекст:** Таблица [`crm_entity_field_values`](../../app/models/bitrix.py) и метод [`CrmRepository.replace_field_values`](../../app/repositories/crm_repository.py) существуют, но [`ImportOrchestrator`](../../app/services/bitrix_import/orchestrator.py) **не вызывает** их. [`crm_child_records`](../../app/models/bitrix.py) — schema-only.

---

## Решение

### Фазирование

| Фаза | Query compiler | Import |
|------|----------------|--------|
| **MVP (intelligent export v1)** | `CrmEntity` denorm columns + `raw_payload` JSONB paths | без изменений |
| **v1.1** | + фильтры/sort по `crm_entity_field_values` | populate on upsert |
| **v2** | joins через child records | import activities, stage history |

### MVP: достаточно `raw_payload` + denorm

**Denormalized columns** (уже в `crm_entities`):
`title`, `category_id`, `stage_id`, `assigned_by_id`, `amount`, `source_id`, `currency_id`, `created_time`, `updated_time`, `closed_at`.

**JSONB access** (PostgreSQL):
```sql
raw_payload->>'STAGE_ID'
raw_payload @> '{"UF_CRM_...": "value"}'
```

Compiler whitelist maps catalog field → `{source: "column"|"jsonb", path: "..."}`.

**Ограничения MVP:**
- Фильтры по multifield (PHONE arrays) — через transform post-fetch или simplified text cast.
- Сложные relation joins — только explicit plan joins по известным ID-полям.

### v1.1: populate `crm_entity_field_values` (рекомендовано до сложных отчётов)

Добавить в `ImportOrchestrator._persist_entity` после `upsert_entity`:

1. Load active `CrmFieldDefinition` for entity_type_id.
2. Extract values from payload per field_type (enumeration → dictionary_entry_id, user → related_entity, etc.).
3. Call `replace_field_values`.

**Приоритет полей:** все `is_active` definitions; skip file/address on v1.1.

**Оценка трудозатрат:** средняя (1–2 недели) — отдельная задача импорта, **не блокер MVP**.

### `crm_child_records` — отложить

Импорт `crm.activity.list`, `crm.stagehistory.list` — **не нужен** для MVP (deals/leads/contacts/companies sheets).

Включить когда потребуются листы «История стадий» / «Активности».

---

## Матрица: нужно ли нормализовать до query compiler?

| Возможность | MVP без field_values | С field_values |
|-------------|---------------------|----------------|
| Filter STAGE_ID, CATEGORY_ID | denorm columns | either |
| Filter UF_CRM_* | JSONB path | indexed column |
| Sort by custom UF | JSONB (slow) | numeric_value/date_value |
| Group by dictionary label | join dictionaries via external_id in JSONB | dictionary_entry_id FK |
| Phone normalization export | transform engine | text_value |

**Вывод:** MVP **можно** запускать без доработки импорта; **v1.1 field_values обязательна** при >10k entities и частых UF-фильтрах.

---

## Последствия

- Compiler в MVP содержит `JsonbFieldResolver` — явный mapping, не generic JSONPath от AI.
- Performance risk на больших JSONB фильтрах — мониторинг + индекс GIN на `raw_payload` (optional migration).

## Action items (import team)

1. `orchestrator.py` — hook `FieldValueExtractor.extract(entity, definitions)` → `replace_field_values`.
2. Unit tests with fixtures from [`tests/fixtures/bitrix_import_fixtures.py`](../../tests/fixtures/bitrix_import_fixtures.py).
3. Backfill job: `mode=field_values_backfill` one-time sync run.
