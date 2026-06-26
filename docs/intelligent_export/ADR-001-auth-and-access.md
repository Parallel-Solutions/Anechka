# ADR-001: Аутентификация, RBAC и права на данные

**Статус:** Принято  
**Дата:** 2026-06-25  
**Контекст:** [`API.md`](../../API.md) L17 — «Аутентификация: Нет». `APP_SECRET_KEY` в [`config.py`](../../app/config.py) не используется. Bitrix ACL после импорта не применяется.

---

## Решение

### 1. Модель пользователей приложения

Вводится таблица `app_users` (см. ADR-005), **отдельная** от `crm_users` (зеркало Bitrix).

| Поле | Назначение |
|------|------------|
| `id` | PK |
| `email` / `login` | Уникальный идентификатор |
| `password_hash` | bcrypt (или argon2) |
| `display_name` | UI |
| `role` | `admin` \| `analyst` \| `viewer` |
| `crm_user_external_id` | Опциональная связь с `crm_users.external_id` для scope |
| `is_active` | Блокировка |
| `portal_id` | Изоляция портала (как у CRM-таблиц) |

**Фаза 1 (MVP):** cookie-сессия на `APP_SECRET_KEY` + signed session token (itsdangerous / starlette SessionMiddleware).  
**Фаза 2 (опционально):** Bitrix OAuth для SSO — не блокирует MVP.

### 2. RBAC — три роли

| Роль | Intelligent export | Admin CRM import | Settings | Memory (project) |
|------|-------------------|------------------|----------|------------------|
| `admin` | full | full | full | read/write |
| `analyst` | create/run/preview | read | read | read |
| `viewer` | preview only (scoped) | read (scoped) | — | read |

Проверка роли — FastAPI dependency `require_role("analyst")` в новом router `app/routers/intelligent_export.py`.

### 3. Bitrix ACL → локальные права

**Факт:** права Bitrix (видимость сделок по отделам, «мои» vs «все») **не импортируются** в PostgreSQL ([`orchestrator.py`](../../app/services/bitrix_import/orchestrator.py) не сохраняет ACL).

**Решение — app-level data scope (не реплика Bitrix ACL):**

| Scope | Правило для `viewer` | Правило для `analyst`/`admin` |
|-------|---------------------|-------------------------------|
| `assigned_only` | `crm_entities.assigned_by_id = app_users.crm_user_external_id` | без ограничения по ответственному |
| `entity_types` | whitelist типов (default: deal, contact) | все импортированные типы |
| `denied_fields` | список field_code из project memory | admin может override |
| `max_rows` | `min(plan.limit, role_limit)` | `min(plan.limit, max_export_size)` |

**Критический риск (явно принят):** локальная выгрузка **не эквивалентна** Bitrix ACL. В UI и audit логируется предупреждение: «Доступ определяется ролью приложения, не правами Bitrix24».

**Mitigation:**
- Привязка `app_users.crm_user_external_id` к Bitrix user ID вебхука/сотрудника.
- Опциональный режим `strict_bitrix_scope`: для preview дополнительно вызывать `crm.item.list` с тем же filter (только COUNT/IDs) — **фаза 2**, не MVP.

### 4. Изоляция ресурсов

| Ресурс | Правило |
|--------|---------|
| Диалоги (`ie_conversations`) | `user_id = current_user.id` |
| ExportPlan versions | через conversation ownership |
| Файлы выгрузки | `export_jobs` + проверка `created_by_user_id` (новая колонка) |
| Download URL | signed token или session + job ownership |
| Project memory | `portal_id` + role `admin` для write |

### 5. Middleware

```
Request → SessionAuthMiddleware → get_current_user (optional on public pages)
       → RoleChecker (on /api/intelligent-export/*)
       → ExportScopeEnforcer (in Validator/Compiler)
```

Публичные без auth (до миграции): `/`, `/settings` — **временно**; все `/api/intelligent-export/*` — **только authenticated**.

### 6. Миграция с текущего состояния

1. Добавить `app_users` + seed admin из env `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD`.
2. Обернуть `/admin/bitrix/*` в `require_role("admin")`.
3. Legacy AI chat (`/api/ai/chat`) — `require_role("analyst")` или read-only mode.
4. Обновить [`API.md`](../../API.md) — убрать «Аутентификация: Нет».

---

## Последствия

- **(+)** Блокер безопасности снят для production multi-user.
- **(-)** Bitrix ACL не воспроизводится полностью — документировать для пользователей.
- **(-)** Требуется bootstrap admin и миграция UX (login page).

## Альтернативы (отклонены)

| Альтернатива | Причина отклонения |
|--------------|-------------------|
| Оставить без auth (VPN only) | Не масштабируется; не защищает диалоги/файлы |
| Полная реплика Bitrix ACL | Нет данных в импорте; высокая сложность |
| API key per user | Нет UX для memory/dialog ownership |
