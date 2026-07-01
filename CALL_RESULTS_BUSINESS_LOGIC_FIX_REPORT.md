# Call Results Business Logic Fix Report

## 1. Старая реализация

- Одна категория (`hot_lead`, `manager_callback`, `robot_callback`, `refusal`, `unknown`)
- Comment для hot_lead/manager_callback/refusal; todo для manager_callback; tasks.task.add для RFP
- No Answer/Voicemail → `robot_callback` + skip_reason
- Execute endpoint — заглушка 501
- Нет retry queue, contact search, execution journal
- UI read-only для большинства полей

## 2. Противоречия утверждённому ТЗ

| Было | Стало |
|------|-------|
| hot_lead → comment + task | positive → только crm.activity.todo.add |
| manager_callback → comment + todo | alternate_contact / callback_later → contact flow / retry queue |
| robot_callback для No Answer | unsupported_outcome → manual review |
| tasks.task.add | удалено из planner |
| execute 501 | реальный execute за feature flag |

## 3. Изменённые файлы (основные)

- `app/models/call_results.py` — signals, queues, execution fields
- `app/services/call_results/signal_merger.py`, `llm_schema.py`, `deterministic_pre_classifier.py`
- `app/services/call_results/action_planner.py`, `payload_builder.py`, `payload_validator.py`
- `app/services/call_results/orchestrator.py`, `crm_action_service.py`
- `app/services/call_results/bitrix_gateway.py`, `fake_bitrix_gateway.py`
- `app/services/call_results/retry_queue_gateway.py`, `contact_search_gateway.py`
- `app/routers/call_results.py`, `app/static/js/call_results.js`
- `app/templates/call_result_import.html`, `app/config.py`, `.env.example`
- Tests: `test_call_result_action_planner_v2.py`, `test_call_result_regression.py`, и др.

## 4. Миграции

- `alembic/versions/a1b2c3d4e5f6_call_results_business_signals.py`

## 5. Матрица сигналов и действий

| Сигнал | CRM todo | Comment | Contact | Link | Retry | Contact search |
|--------|----------|---------|---------|------|-------|----------------|
| positive | да | нет | нет | нет | нет* | нет |
| alternate_contact | нет | нет | да | да | да | нет |
| callback_later | нет | нет | нет | нет | да | нет |
| explicit_refusal | нет | да | нет | нет | нет | нет |
| hangup_without_result | нет | нет | нет | нет | после поиска | да |

## 6. Методы Bitrix

- `crm.activity.todo.add` — положительный результат
- `crm.timeline.comment.add` — отказ
- `crm.contact.list/add/update` — новый контакт
- `crm.deal.contact.add` — привязка к сделке

## 7. Признак нового контакта

- `BITRIX_CALL_SOURCE_FIELD_CODE` / `BITRIX_CALL_SOURCE_FIELD_VALUE` в `.env`
- Проверка через `ContactMarkerValidator` и `/api/call-results/diagnostics`

## 8. Retry queue

- Таблица `call_retry_queue_entries`
- Gateway: `RetryQueueGateway.add()` с idempotency key
- API: `GET /api/call-results/retry-queue`, export CSV

## 9. Contact search queue

- Таблица `call_contact_search_entries`
- Создаётся при hangup; confirm → retry queue
- API: `GET /api/call-results/contact-search`, `POST .../confirm`

## 10. Execute

- `CALL_RESULTS_BITRIX_EXECUTION_ENABLED=true`
- `POST /api/call-results/imports/{id}/execute` с `confirmation_token=EXECUTE`
- Фоновый job через `CallResultJobService.submit_execute`

## 11. Тесты

```bash
docker compose exec web pytest tests/test_call_result_*.py tests/test_call_results_api.py -v
```

## 12. Ручная настройка Bitrix24

1. Создать UF-поле контакта «получен в ходе автоматического обзвона Анечкой»
2. Указать `BITRIX_CALL_SOURCE_FIELD_CODE` и `VALUE`
3. Webhook с правами CRM
4. Включить execute только после проверки preview

## 13. Ограничения

- Tomoru export (`sent_to_tomoru`) — stub
- Contact search provider по умолчанию `fake`
- Docker-образ требует rebuild для применения кода (нет bind-mount app/)
