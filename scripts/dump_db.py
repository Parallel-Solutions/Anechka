#!/usr/bin/env python3
"""Dump PostgreSQL database to database.sql with UTF-8 encoding.

Avoids Windows shell redirect encoding issues by running pg_dump inside Docker.

Run:
  docker compose exec web python scripts/dump_db.py
  docker compose exec web python scripts/dump_db.py --output /seed/database.sql
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


def database_url() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://bitrix:bitrix@db:5432/bitrix_export",
    )


def pg_env_from_url(url: str) -> dict[str, str]:
    parsed = urlparse(url.replace("postgresql+psycopg://", "postgresql://"))
    env = os.environ.copy()
    env.setdefault("PGHOST", parsed.hostname or "db")
    env.setdefault("PGPORT", str(parsed.port or 5432))
    env.setdefault("PGUSER", parsed.username or os.environ.get("POSTGRES_USER", "bitrix"))
    env.setdefault("PGPASSWORD", parsed.password or os.environ.get("POSTGRES_PASSWORD", "bitrix"))
    env.setdefault("PGDATABASE", parsed.path.lstrip("/") or os.environ.get("POSTGRES_DB", "bitrix_export"))
    env["PGCLIENTENCODING"] = "UTF8"
    return env


def default_output_path() -> Path:
    mounted = Path("/app/database.sql")
    if mounted.parent.exists():
        return mounted
    return Path(__file__).resolve().parent.parent / "database.sql"


def main() -> int:
    parser = argparse.ArgumentParser(description="Dump PostgreSQL database to UTF-8 SQL file.")
    parser.add_argument(
        "--output",
        type=Path,
        default=default_output_path(),
        help="Output SQL file path (default: bitrix_export_web/database.sql)",
    )
    args = parser.parse_args()

    output_path = args.output.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    env = pg_env_from_url(database_url())

    cmd = [
        "pg_dump",
        "--encoding=UTF8",
        "--no-owner",
        "--no-privileges",
        "-f",
        str(output_path),
    ]
    print(f"dump_db: writing {output_path} ...")
    result = subprocess.run(cmd, env=env, check=False)
    if result.returncode != 0:
        print("dump_db: pg_dump failed", file=sys.stderr)
        return result.returncode

    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"dump_db: complete ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
