# Intelligent Export — архитектурная документация

**Полный отчёт в одном файле:** [INTELLIGENT_EXPORT_REPORT.md](../INTELLIGENT_EXPORT_REPORT.md)

**Эксплуатация, API, env, роли, безопасность:** [USAGE.md](USAGE.md)

Документы ниже — исходные ADR (также включены в отчёт).

## Цепочка безопасности (целевая)

```
Запрос пользователя
  → диалог с AI (ExportPlanPlanner)
  → формальный ExportPlan JSON
  → ExportPlanValidator (сервер)
  → QueryCompiler → локальная PostgreSQL
  → TransformationEngine
  → ValidationEngine
  → Excel/CSV Output
  → история запуска + файл
```

AI **не** выполняет SQL и **не** вызывает Bitrix REST напрямую для выгрузок — только формирует `ExportPlan` из разрешённого каталога полей.

## ADR (Architecture Decision Records)

| ID | Тема | Файл |
|----|------|------|
| ADR-001 | Аутентификация, RBAC, права на данные | [ADR-001-auth-and-access.md](ADR-001-auth-and-access.md) |
| ADR-002 | Источник данных (local DB vs live Bitrix) | [ADR-002-data-source.md](ADR-002-data-source.md) |
| ADR-003 | Нормализация импорта (`crm_entity_field_values`) | [ADR-003-normalized-import.md](ADR-003-normalized-import.md) |
| ADR-004 | ExportPlan: схема, validator, query compiler | [ADR-004-export-plan-domain.md](ADR-004-export-plan-domain.md) |
| ADR-005 | Персистентность: диалоги, планы, память | [ADR-005-persistence.md](ADR-005-persistence.md) |
| ADR-006 | Job pipeline: preview и long-running export | [ADR-006-job-pipeline.md](ADR-006-job-pipeline.md) |

## Артефакты реализации (фаза 0)

| Артефакт | Путь |
|----------|------|
| JSON Schema ExportPlan | [export_plan.schema.json](export_plan.schema.json) |
| Pydantic-модели + validator + compiler | [`app/services/export_plan/`](../../app/services/export_plan/) |
| SQLAlchemy-модели (новые таблицы) | [`app/models/intelligent_export.py`](../../app/models/intelligent_export.py) |
| Alembic migration | [`alembic/versions/3a1c_export_plan_schema.py`](../../alembic/versions/3a1c_export_plan_schema.py) |
| Тесты validator/compiler | [`tests/test_export_plan.py`](../../tests/test_export_plan.py) |

## Связь с существующим кодом

- **Не заменяет** [`AIService`](../../app/services/ai_service.py) (live Bitrix chat) — intelligent export — отдельный контур.
- **Переиспользует** [`JobService`](../../app/services/job_service.py), [`ExcelService`](../../app/services/excel_service.py), [`CrmRepository`](../../app/repositories/crm_repository.py), [`BitrixMetadataAIService`](../../app/services/bitrix_import/metadata_ai_service.py) (паттерн structured output).
