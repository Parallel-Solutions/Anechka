"""Smoke test: full Alembic migration chain applies on a clean database."""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

ROOT = Path(__file__).resolve().parent.parent

NEW_TABLES = {
    "app_users",
    "ie_conversations",
    "ie_messages",
    "ie_export_plan_versions",
    "ie_memory_entries",
    "ie_export_runs",
    "ie_audit_log",
}


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def test_migration_chain_applies_on_clean_db(tmp_path):
    db_file = tmp_path / "migrate_smoke.sqlite"
    db_url = f"sqlite:///{db_file}"
    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url
    try:
        cfg = _alembic_config(db_url)
        command.upgrade(cfg, "head")

        engine = create_engine(db_url)
        try:
            tables = set(inspect(engine).get_table_names())
        finally:
            engine.dispose()

        assert NEW_TABLES.issubset(tables), f"missing: {NEW_TABLES - tables}"

        # export_jobs extended columns from revision 3a1c9e2f4b01
        engine = create_engine(db_url)
        try:
            cols = {c["name"] for c in inspect(engine).get_columns("export_jobs")}
        finally:
            engine.dispose()
        assert {"created_by_user_id", "plan_version_id"}.issubset(cols)

        # ie_memory_entries workflow columns from revision 4b2d7c1a9f02
        engine = create_engine(db_url)
        try:
            mem_cols = {c["name"] for c in inspect(engine).get_columns("ie_memory_entries")}
        finally:
            engine.dispose()
        assert {"status", "priority", "source", "approved_by_user_id"}.issubset(mem_cols)

        # downgrade to the initial schema should drop all intelligent export tables
        command.downgrade(cfg, "2b32241187b3")
        engine = create_engine(db_url)
        try:
            tables_after = set(inspect(engine).get_table_names())
        finally:
            engine.dispose()
        assert not NEW_TABLES.intersection(tables_after)
    finally:
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev
