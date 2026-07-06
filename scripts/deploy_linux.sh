#!/usr/bin/env bash
# Deploy bitrix_export_web on Linux (Docker Compose production stack).
# Run from bitrix_export_web/: bash scripts/deploy_linux.sh
#
# One command brings up the full stack: db -> db-restore -> migrate -> web/worker.
# Seed restore runs only on an empty database (existing pgdata volume is preserved).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Prod stack only — ignore COMPOSE_FILE from .env (local dev may set docker-compose.dev.yml)
COMPOSE=(docker compose -f docker-compose.yml)

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!!]${NC} $*"; }
fail()  { echo -e "${RED}[ERR]${NC} $*" >&2; exit 1; }

echo "=== Bitrix Export Web — Linux deploy ==="
echo "Working directory: $ROOT"
echo

# 1. Docker
if ! command -v docker >/dev/null 2>&1; then
  fail "Docker не установлен. См. DEPLOY_LINUX.md §2: curl -fsSL https://get.docker.com | sh"
fi
if ! docker compose version >/dev/null 2>&1; then
  fail "Docker Compose v2 не найден. Установите Docker Engine с Compose plugin."
fi
info "Docker $(docker --version | cut -d' ' -f3 | tr -d ',')"
info "Compose $(docker compose version --short 2>/dev/null || docker compose version | head -1)"

# 2. Git LFS / seed dump
if [[ -f database.sql ]]; then
  size=$(stat -c%s database.sql 2>/dev/null || stat -f%z database.sql 2>/dev/null || echo 0)
  if [[ "$size" -lt 1000000 ]]; then
    if head -1 database.sql 2>/dev/null | grep -q 'git-lfs'; then
      fail "database.sql — указатель Git LFS. Выполните: git lfs pull"
    fi
    warn "database.sql меньше 1 MB — возможно, дамп не скачан (git lfs pull)"
  else
    info "database.sql найден ($(numfmt --to=iec-i --suffix=B "$size" 2>/dev/null || echo "${size} bytes"))"
  fi
else
  warn "database.sql отсутствует — restore пропустит seed на пустой БД"
fi

# 3. .env
if [[ ! -f .env ]]; then
  cp .env.example .env
  warn "Создан .env из .env.example — задайте секреты перед production!"
  warn "  APP_SECRET_KEY, POSTGRES_PASSWORD, BASIC_AUTH_PASSWORD, BOOTSTRAP_ADMIN_PASSWORD"
else
  info ".env существует"
fi

if grep -qE '^(APP_SECRET_KEY|POSTGRES_PASSWORD|BASIC_AUTH_PASSWORD|BOOTSTRAP_ADMIN_PASSWORD)=change-me' .env 2>/dev/null; then
  warn "В .env остались значения change-me — смените секреты для production"
fi

# 3b. Alembic head vs seed dump revision
if [[ -d alembic/versions ]] && [[ -f database.sql ]]; then
  seed_rev=$(sed -n '/COPY public\.alembic_version/,/^\\\./p' database.sql 2>/dev/null \
    | grep -E '^[0-9a-f]+$' | head -1 || true)
  all_revs=$(grep -h '^revision:' alembic/versions/*.py 2>/dev/null \
    | sed -E 's/.*"([^"]+)".*/\1/' || true)
  down_revs=$(grep -h '^down_revision:' alembic/versions/*.py 2>/dev/null \
    | sed -E 's/.*"([^"]+)".*/\1/' | grep -v None || true)
  code_head=""
  for rev in $all_revs; do
    if ! echo "$down_revs" | grep -qx "$rev"; then
      code_head="$rev"
      break
    fi
  done
  if [[ -n "$seed_rev" && -n "$code_head" && "$seed_rev" != "$code_head" ]]; then
    fail "Несовпадение ревизий: database.sql=$seed_rev, код (head)=$code_head. Выполните git pull && git lfs pull"
  elif [[ -n "$seed_rev" && -n "$code_head" ]]; then
    info "Alembic head совпадает с seed-дампом ($code_head)"
  fi
fi

# 4. Start stack
echo
echo "=== docker compose -f docker-compose.yml up --build -d ==="
"${COMPOSE[@]}" up --build -d

# 5. db-restore verification
echo
echo "=== Проверка db-restore ==="
restore_logs=$("${COMPOSE[@]}" logs db-restore 2>/dev/null || true)
if echo "$restore_logs" | grep -q 'RESULT=restored'; then
  info "db-restore: seed дамп накатан (RESULT=restored)"
elif echo "$restore_logs" | grep -q 'RESULT=skipped'; then
  info "db-restore: restore пропущен — база уже содержит данные или seed отсутствует"
elif echo "$restore_logs" | grep -q 'restore complete'; then
  info "db-restore: seed дамп накатан"
elif echo "$restore_logs" | grep -q 'skipping restore\|seed file not found'; then
  info "db-restore: restore пропущен"
else
  fail "db-restore: не удалось определить результат. Логи: ${COMPOSE[*]} logs db-restore"
fi

# 6. migrate verification
echo
echo "=== Проверка migrate ==="
migrate_status=$("${COMPOSE[@]}" ps -a migrate --format '{{.State}}' 2>/dev/null | head -1 || true)
if [[ "$migrate_status" != "exited" ]]; then
  fail "migrate: контейнер не завершился (state=$migrate_status). Логи: ${COMPOSE[*]} logs migrate"
fi
migrate_exit=$("${COMPOSE[@]}" ps -a migrate --format '{{.ExitCode}}' 2>/dev/null | head -1 || true)
if [[ "$migrate_exit" != "0" ]]; then
  hint=""
  if [[ "$migrate_exit" == "3" ]]; then
    hint=" (Alembic CommandError — см. DEPLOY_LINUX.md §11: Can't locate revision / git pull + rebuild)"
  fi
  fail "migrate: exit code $migrate_exit.$hint Логи: ${COMPOSE[*]} logs migrate"
fi
info "migrate: alembic upgrade head завершён успешно"

WEB_PORT="${WEB_PUBLISH_PORT:-80}"
if [[ -f .env ]]; then
  env_port=$(grep -E '^WEB_PUBLISH_PORT=' .env 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '[:space:]' || true)
  if [[ -n "$env_port" ]]; then
    WEB_PORT="$env_port"
  fi
fi

# 7. Health check
echo
echo "=== Ожидание /health ==="
for i in $(seq 1 30); do
  if curl -sf "http://localhost:${WEB_PORT}/health" >/dev/null 2>&1; then
    body=$(curl -s "http://localhost:${WEB_PORT}/health")
    info "Health: $body"
    break
  fi
  if [[ "$i" -eq 30 ]]; then
    fail "Health check не прошёл за 30 попыток. Логи: ${COMPOSE[*]} logs web"
  fi
  sleep 2
done

# 8. DB sanity check
echo
echo "=== Проверка данных в БД ==="
crm_count=$("${COMPOSE[@]}" exec -T web python -c "
from sqlalchemy import create_engine, text
import os
engine = create_engine(os.environ['DATABASE_URL'])
with engine.connect() as conn:
    print(conn.execute(text('SELECT count(*) FROM crm_entities')).scalar())
" 2>/dev/null | tr -d '[:space:]' || echo "0")
if [[ "$crm_count" =~ ^[0-9]+$ ]] && [[ "$crm_count" -gt 0 ]]; then
  info "crm_entities: $crm_count записей"
else
  warn "crm_entities пуста или недоступна (count=$crm_count) — проверьте seed/import"
fi

echo
"${COMPOSE[@]}" ps
echo
host_ip=$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'localhost')
if [[ "$WEB_PORT" == "80" ]]; then
  info "Приложение: http://${host_ip}"
else
  info "Приложение: http://${host_ip}:${WEB_PORT}"
fi
info "Логин: значения BASIC_AUTH_USERNAME / BASIC_AUTH_PASSWORD из .env"
echo
warn "Следующий шаг: откройте /settings и укажите BITRIX_WEBHOOK_URL, затем /bitrix-import при необходимости."
