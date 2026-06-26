# Bitrix24 Export Web

Веб-приложение для выгрузки сделок из Bitrix24 в Excel с телефонами и контактами.

## Назначение

Приложение поддерживает два режима:

1. **Выгрузка по региону** — поиск региона в инфоблоке, фильтрация сделок по пользовательскому полю.
2. **Выгрузка по стадии** — выбор воронки и стадии, исключение сотрудников по ID.

Результат сохраняется в XLSX. Выгрузка выполняется в фоне с отображением прогресса.

- **По региону** — формат как в `tel_po_reg.py` (лист «Сделки», одна строка на сделку).
- **По стадии** — нормализованный или широкий формат на выбор.

## Системные требования

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) или Docker Engine с Compose
- Доступ к Bitrix24 REST API (входящий вебхук)
- OpenAI API (опционально, для AI-анализа метаданных полей)

## Установка и запуск (production)

```bash
cd bitrix_export_web
copy .env.example .env   # Windows
# cp .env.example .env   # Linux / macOS
```

Задайте в `.env` надёжные значения: `APP_SECRET_KEY`, `POSTGRES_PASSWORD`, `BASIC_AUTH_PASSWORD`.

```bash
docker compose up --build -d
```

Откройте в браузере: **http://localhost:8000** — браузер запросит логин и пароль из `BASIC_AUTH_USERNAME` / `BASIC_AUTH_PASSWORD`.

Сервисы: `db` → `db-restore` (восстановление `database.sql` на пустой БД) → `migrate` → `web`, `worker`.

При первом запуске на пустом volume дамп [`database.sql`](database.sql) восстанавливается автоматически. При повторном запуске с существующими данными restore пропускается.

> **Клонирование репозитория:** `database.sql` хранится в Git LFS (~1.3 GB). После `git clone` выполните `git lfs pull`.

Данные сохраняются в Docker volumes (`pgdata`, `filestorage`) и каталогах `exports/`, `logs/` на хосте.

Остановка: `docker compose down` (данные в volumes сохраняются). Полный сброс: `docker compose down -v`.

### Режим разработки (hot-reload)

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

Dev-override включает `--reload`, mount исходников и публикацию PostgreSQL на `localhost:5433`.

> ### Запускайте ТОЛЬКО через Docker — одна копия, одна база
>
> Приложение должно работать единственным экземпляром — стеком Docker (`docker compose up -d`).
> **Не запускайте параллельно локальный `uvicorn`/`run.py` на хосте.** Если на `127.0.0.1:8000`
> поднят локальный процесс, он перехватывает запросы браузера у Docker-контейнера и обычно
> ходит в другую (часто пустую) базу — тогда «ничего не выгружается», а диалоги и выгрузки
> «исчезают». Симптом одной из таких ситуаций уже устранялся: лишний хостовый `run.py` шёл в
> пустой хостовый PostgreSQL.
>
> - База Docker в prod-режиме **не** опубликована на хост (только внутри docker-сети).
>   Для доступа с хоста используйте dev-override (`docker-compose.dev.yml`) — PostgreSQL на **`localhost:5433`**.
> - При старте `web` пишет в лог строку вида `Startup DB check: db=... crm_entities=N` — по ней
>   сразу видно, к какой базе подключились и есть ли данные.
> - Если страница **Умные выгрузки** показывает баннер «База пуста», значит в подключённой БД нет
>   импортированных данных CRM — сначала выполните импорт.
>
> Проверить, что порт 8000 занят именно Docker, а не лишним локальным процессом (Windows PowerShell):
>
> ```powershell
> Get-NetTCPConnection -LocalPort 8000 -State Listen |
>   ForEach-Object { (Get-Process -Id $_.OwningProcess).ProcessName }
> # ожидается только com.docker.backend; python.exe здесь быть не должно
> ```

## Настройка `.env`

```env
# BITRIX_WEBHOOK_URL=   # необязательно; обычно задаётся на странице Настройки
APP_SECRET_KEY=change-me
BASIC_AUTH_USERNAME=admin
BASIC_AUTH_PASSWORD=change-me
DATABASE_URL=postgresql+psycopg://bitrix:change-me@db:5432/bitrix_export
POSTGRES_USER=bitrix
POSTGRES_PASSWORD=change-me
POSTGRES_DB=bitrix_export
EXPORT_DIR=./exports
FILE_STORAGE_DIR=./filestorage
LOG_LEVEL=INFO
OPENAI_BITRIX_METADATA_MODEL=
```

Полный список переменных: [.env.example](.env.example) и [BITRIX_IMPORT.md](BITRIX_IMPORT.md).

Приоритет настроек: сохранённые в приложении (страница **Настройки**) → переменные окружения → значения по умолчанию.
Переменные в `.env` используются как начальные значения, пока параметр не сохранён через интерфейс.

Дополнительные параметры (тайм-ауты, повторы, лимиты) настраиваются на странице **Настройки** в веб-интерфейсе.

## Операции через Docker

| Операция | Команда |
|----------|---------|
| Запуск (prod) | `docker compose up --build -d` |
| Запуск (dev, hot-reload) | `docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build` |
| Остановка | `docker compose down` |
| Миграции (вручную) | `docker compose run --rm migrate` |
| Тесты | `docker compose exec web pytest` |
| Seed users | `docker compose exec web python scripts/seed_test_users.py` |
| Очистка CRM | `docker compose exec web python scripts/clear_crm_import_data.py` |
| Перезапуск worker | `docker compose restart worker` |
| Rebuild после смены deps | `docker compose build && docker compose up -d` |

### CRM Import UI

**http://localhost:8000/bitrix-import** — dashboard, запуск импорта, просмотр данных.

Подробнее: **[BITRIX_IMPORT.md](BITRIX_IMPORT.md)**

Документация API: **http://localhost:8000/docs**

Полная спецификация HTTP-ручек и методов Bitrix24: **[API.md](API.md)**

## Работа с интерфейсом

1. Укажите вебхук Bitrix24 в настройках и проверьте подключение.
2. На главной странице выберите вкладку «По региону» или «По стадии».
3. Заполните параметры и запустите выгрузку.
4. На странице задачи отслеживайте прогресс (SSE или polling).
5. После завершения скачайте XLSX.

## Структура Excel

### Выгрузка по региону (tel_po_reg)

Один лист **Сделки**. Одна строка — одна сделка. Столбцы: `ID Сделки`, `Название сделки`, пары `ФИО контакта N` / `Телефон контакта N`. Уникальные телефоны: сначала телефон компании, затем контакты сделки и компании.

### Выгрузка по стадии — нормализованный формат

Одна строка — один телефон/контакт. Столбцы: ID сделки, название, категория, стадия, ответственный, компания, контакт, телефоны, источник, регион, дата.

### Выгрузка по стадии — широкий формат

Одна строка — одна сделка. Столбцы: сотрудник, ID сделки, название, регион, пары «ФИО / телефон».

На листе **Информация** — параметры запуска и статистика.

## Тесты

```bash
docker compose exec web pytest
```

## Безопасность

- Вебхук не хранится в исходном коде.
- В интерфейсе URL маскируется (`/rest/***/***`).
- Телефоны в логах маскируются.
- Скачивание файлов защищено от path traversal.
- Токен не попадает в историю задач.

## Типичные ошибки

| Сообщение | Решение |
|-----------|---------|
| Нет подключения к Bitrix24 | Проверьте URL вебхука в настройках |
| Регион не найден | Уточните название и ID инфоблока |
| Несколько регионов | Выберите нужный из списка |
| Нет сделок | Проверьте категорию, регион или стадию |
| Лимит запросов | Подождите и повторите позже |

## Структура проекта

```
bitrix_export_web/
├── app/                 # FastAPI приложение
│   ├── models/          # SQLAlchemy models (legacy + bitrix)
│   ├── repositories/    # sync + CRM repositories
│   ├── services/bitrix_import/  # import orchestrator, AI, discovery
│   ├── routers/admin_bitrix.py  # admin API
│   └── worker.py        # background import worker
├── alembic/             # DB migrations
├── exports/             # Готовые XLSX
├── filestorage/         # Импортированные файлы Bitrix
├── logs/                # Журналы
├── tests/               # pytest
├── Dockerfile
├── docker-compose.yml   # db + migrate + web + worker
└── requirements.txt
```

Справочные исходники: `tel_po_reg.py`, `tel_po_stadii.py` (в корне workspace).
