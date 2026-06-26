#!/usr/bin/env python3
"""Restore database.sql into PostgreSQL when the database is empty."""

from __future__ import annotations

import os
import subprocess
import sys
import time


def run_psql(args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("PGHOST", "db")
    env.setdefault("PGPORT", "5432")
    env.setdefault("PGUSER", os.environ.get("POSTGRES_USER", "bitrix"))
    env.setdefault("PGPASSWORD", os.environ.get("POSTGRES_PASSWORD", "bitrix"))
    env.setdefault("PGDATABASE", os.environ.get("POSTGRES_DB", "bitrix_export"))
    return subprocess.run(
        ["psql", *args],
        env=env,
        check=False,
        text=True,
        capture_output=capture,
    )


def wait_for_db(timeout_seconds: int = 120) -> None:
    host = os.environ.get("PGHOST", "db")
    port = os.environ.get("PGPORT", "5432")
    user = os.environ.get("POSTGRES_USER", "bitrix")
    db = os.environ.get("POSTGRES_DB", "bitrix_export")
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        result = subprocess.run(
            ["pg_isready", "-h", host, "-p", port, "-U", user, "-d", db],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    raise SystemExit(f"db-restore: PostgreSQL not ready after {timeout_seconds}s")


def scalar(query: str) -> int:
    result = run_psql(["-tAc", query], capture=True)
    if result.returncode != 0:
        return 0
    return int(result.stdout.strip() or "0")


def main() -> None:
    seed_file = os.environ.get("SEED_FILE", "/seed/database.sql")
    print("db-restore: waiting for PostgreSQL...")
    wait_for_db()

    table_count = scalar(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE';"
    )
    print(f"db-restore: public tables count={table_count}")

    if table_count > 0:
        crm_count = scalar("SELECT count(*) FROM crm_entities;")
        print(f"db-restore: crm_entities count={crm_count}")
        if crm_count > 0:
            print("db-restore: database already has data, skipping restore")
            return

    if not os.path.isfile(seed_file):
        print(f"db-restore: seed file not found at {seed_file}, skipping restore")
        return

    print(f"db-restore: restoring from {seed_file}...")
    result = run_psql(["-v", "ON_ERROR_STOP=1", "-f", seed_file])
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    print("db-restore: restore complete")


if __name__ == "__main__":
    main()
