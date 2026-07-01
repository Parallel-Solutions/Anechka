#!/usr/bin/env python3
"""Fix UTF-8 mojibake stored in PostgreSQL text/json columns.

Typical corruption: UTF-8 bytes were misread as CP866 and saved as Unicode
(box-drawing chars like U+2568 mixed with Cyrillic).

Run:
  docker compose exec web python scripts/fix_mojibake.py --dry-run
  docker compose exec web python scripts/fix_mojibake.py
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.database import SessionLocal

TEXT_TYPES = frozenset({"text", "character varying", "varchar", "character", "char"})
JSON_TYPES = frozenset({"json", "jsonb"})
MOJIBAKE_ENCODINGS = ("cp866", "cp1252", "latin-1")
BOX_DRAWING = range(0x2500, 0x2580)


def looks_like_mojibake(value: str) -> bool:
    return any(ord(ch) in BOX_DRAWING for ch in value)


def _fix_segment(segment: str) -> str:
    for encoding in MOJIBAKE_ENCODINGS:
        try:
            fixed = segment.encode(encoding).decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
        if fixed != segment:
            return fixed
    return segment


def _longest_fixable_segment(value: str, start: int) -> tuple[str, str]:
    end = start + 1
    best_segment = value[start:end]
    best_fixed = _fix_segment(best_segment)
    while end <= len(value):
        segment = value[start:end]
        try:
            segment.encode("cp866")
        except UnicodeEncodeError:
            break
        fixed = _fix_segment(segment)
        if fixed != segment and not looks_like_mojibake(fixed):
            best_segment = segment
            best_fixed = fixed
        end += 1
    return best_segment, best_fixed


def fix_mojibake(value: str) -> str:
    if not value or not looks_like_mojibake(value):
        return value
    for encoding in MOJIBAKE_ENCODINGS:
        try:
            fixed = value.encode(encoding).decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
        if fixed != value:
            return fixed

    parts: list[str] = []
    index = 0
    while index < len(value):
        if ord(value[index]) not in BOX_DRAWING:
            parts.append(value[index])
            index += 1
            continue
        segment, fixed = _longest_fixable_segment(value, index)
        parts.append(fixed)
        index += len(segment)
    return "".join(parts)


def fix_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return fix_mojibake(value)
    if isinstance(value, dict):
        return {key: fix_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [fix_json_value(item) for item in value]
    return value


def fix_text_value(value: str) -> str:
    if not value:
        return value
    stripped = value.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return fix_mojibake(value)
        fixed = fix_json_value(parsed)
        if fixed != parsed:
            return json.dumps(fixed, ensure_ascii=False)
        return value
    return fix_mojibake(value)


def fix_column_value(value: Any, data_type: str) -> Any:
    if value is None:
        return None
    if data_type in JSON_TYPES:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return fix_text_value(value)
            fixed = fix_json_value(parsed)
            return fixed if fixed != parsed else value
        if isinstance(value, (dict, list)):
            fixed = fix_json_value(value)
            return fixed if fixed != value else value
        return value
    if isinstance(value, str):
        return fix_text_value(value)
    return value


def values_differ(original: Any, fixed: Any, data_type: str) -> bool:
    if data_type in JSON_TYPES and isinstance(original, (dict, list)) and isinstance(fixed, (dict, list)):
        return json.dumps(original, ensure_ascii=False, sort_keys=True) != json.dumps(
            fixed, ensure_ascii=False, sort_keys=True
        )
    return original != fixed


def discover_columns(db, table_filter: str | None) -> list[tuple[str, str, str]]:
    query = text(
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND data_type = ANY(:types)
        ORDER BY table_name, ordinal_position
        """
    )
    allowed = sorted(TEXT_TYPES | JSON_TYPES)
    rows = db.execute(query, {"types": allowed}).all()
    columns: list[tuple[str, str, str]] = []
    for table_name, column_name, data_type in rows:
        if table_filter and table_name != table_filter:
            continue
        columns.append((table_name, column_name, data_type))
    return columns


def discover_primary_key(db, table_name: str) -> list[str]:
    rows = db.execute(
        text(
            """
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.table_schema = 'public'
              AND tc.table_name = :table_name
              AND tc.constraint_type = 'PRIMARY KEY'
            ORDER BY kcu.ordinal_position
            """
        ),
        {"table_name": table_name},
    ).all()
    if not rows:
        raise RuntimeError(f"No primary key found for public.{table_name}")
    return [row[0] for row in rows]


def quote_ident(name: str) -> str:
    return f'"{name.replace(chr(34), chr(34) * 2)}"'


def serialize_for_db(value: Any, data_type: str) -> Any:
    if data_type in JSON_TYPES and isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def set_clause(name: str, data_type: str, idx: int) -> str:
    placeholder = f":val_{idx}"
    if data_type in JSON_TYPES:
        return f"{quote_ident(name)} = CAST({placeholder} AS jsonb)"
    return f"{quote_ident(name)} = {placeholder}"


def process_table(
    db,
    table_name: str,
    columns: Iterable[tuple[str, str]],
    *,
    dry_run: bool,
    sample_limit: int,
    row_limit: int | None,
) -> tuple[int, int]:
    pk_cols = discover_primary_key(db, table_name)
    column_items = list(columns)
    col_index = {column_name: idx for idx, (column_name, _) in enumerate(column_items)}
    col_types = {column_name: data_type for column_name, data_type in column_items}
    pk_select = ", ".join(quote_ident(name) for name in pk_cols)
    col_select = ", ".join(quote_ident(name) for name, _ in column_items)
    sql = f"SELECT {pk_select}, {col_select} FROM {quote_ident(table_name)}"
    result = db.execute(text(sql))

    changed_rows = 0
    scanned_rows = 0
    samples: list[str] = []

    while True:
        batch = result.fetchmany(500)
        if not batch:
            break
        for row in batch:
            if row_limit is not None and scanned_rows >= row_limit:
                break
            scanned_rows += 1
            pk_values = row[: len(pk_cols)]
            updates: dict[str, Any] = {}
            for idx, (column_name, data_type) in enumerate(column_items):
                original = row[len(pk_cols) + idx]
                fixed = fix_column_value(original, data_type)
                if values_differ(original, fixed, data_type):
                    updates[column_name] = fixed

            if not updates:
                continue

            changed_rows += 1
            if len(samples) < sample_limit:
                column_name = next(iter(updates))
                before = str(row[len(pk_cols) + col_index[column_name]])
                after = str(updates[column_name])
                if len(before) > 120:
                    before = before[:117] + "..."
                if len(after) > 120:
                    after = after[:117] + "..."
                sample_cols = ", ".join(sorted(updates))
                samples.append(
                    f"{table_name} pk={pk_values!r} columns=[{sample_cols}]\n"
                    f"  before ({column_name}): {before}\n"
                    f"  after  ({column_name}): {after}"
                )

            if dry_run:
                continue

            update_items = list(updates.items())
            set_parts = [
                set_clause(name, col_types[name], idx) for idx, (name, _) in enumerate(update_items)
            ]
            where_parts = [f"{quote_ident(pk)} = :pk_{idx}" for idx, pk in enumerate(pk_cols)]
            params: dict[str, Any] = {
                f"val_{idx}": serialize_for_db(value, col_types[name])
                for idx, (name, value) in enumerate(update_items)
            }
            for idx, pk_value in enumerate(pk_values):
                params[f"pk_{idx}"] = pk_value
            db.execute(
                text(
                    f"UPDATE {quote_ident(table_name)} SET {', '.join(set_parts)} "
                    f"WHERE {' AND '.join(where_parts)}"
                ),
                params,
            )
        if row_limit is not None and scanned_rows >= row_limit:
            break

    if not dry_run:
        db.commit()

    if samples:
        print(f"\n[{table_name}] sample changes:")
        for sample in samples:
            print(sample)

    return scanned_rows, changed_rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Fix UTF-8 mojibake in PostgreSQL data.")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing.")
    parser.add_argument("--table", help="Process only this table.")
    parser.add_argument("--sample-limit", type=int, default=5, help="Sample changes per table.")
    parser.add_argument("--row-limit", type=int, help="Limit scanned rows per table (testing).")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        columns = discover_columns(db, args.table)
        if not columns:
            print("No text/json columns found.")
            return 0

        by_table: dict[str, list[tuple[str, str]]] = {}
        for table_name, column_name, data_type in columns:
            by_table.setdefault(table_name, []).append((column_name, data_type))

        total_scanned = 0
        total_changed = 0
        mode = "DRY RUN" if args.dry_run else "APPLY"
        print(f"fix_mojibake: mode={mode}, tables={len(by_table)}")

        for table_name in sorted(by_table):
            scanned, changed = process_table(
                db,
                table_name,
                by_table[table_name],
                dry_run=args.dry_run,
                sample_limit=args.sample_limit,
                row_limit=args.row_limit,
            )
            total_scanned += scanned
            total_changed += changed
            print(f"[{table_name}] scanned={scanned}, changed={changed}")

        print(f"Done. scanned={total_scanned}, changed={total_changed}")
        return 0
    except Exception as exc:
        db.rollback()
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
