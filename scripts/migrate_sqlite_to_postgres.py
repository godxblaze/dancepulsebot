"""One-time copy: existing SQLite DB → Postgres (DATABASE_URL).

Usage:
    python -m scripts.migrate_sqlite_to_postgres [--sqlite-path dance.sqlite3]

Table-by-table copy preserving primary keys, then resets each table's identity
sequence to MAX(id)+1. Not a production-grade streaming/chunking tool — the
pilot's data volume is trivial at the time this was written (see
.supergoal/postgres-migration-scale-hardening-dev-t-LdLHP8/ROADMAP.md
assumptions); revisit batching if the pilot has grown significantly.

Never point --sqlite-path at a live bot's dance.sqlite3 while it's running —
this script takes no snapshot (unlike scripts/backup_db.py). Stop the bot or
copy the file first.
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
from datetime import datetime as dt_cls
from pathlib import Path

from sqlalchemy import Boolean, DateTime, text

from app.db.models import Base
from app.db.session import engine

# Insertion order respects FK dependencies (parent before child).
_TABLE_ORDER = (
    "admins",
    "schools",
    "venues",
    "teacher_presets",
    "school_members",
    "school_invites",
    "groups",
    "lesson_cancellations",
    "lesson_place_overrides",
    "persons",
    "memberships",
    "group_admins",
    "support_pool_entries",
    "surveys",
    "responses",
    "support_calls",
    "support_claims",
)


def _read_sqlite_table(conn: sqlite3.Connection, table: str) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
    return [dict(r) for r in rows]


def _coerce_row(sa_table, row: dict) -> dict:
    """sqlite3 hands back raw TEXT/INTEGER — asyncpg wants real datetime/bool
    objects for the corresponding Postgres column types."""
    out = {}
    for col in sa_table.columns:
        if col.name not in row:
            continue
        value = row[col.name]
        if value is not None:
            if isinstance(col.type, DateTime) and isinstance(value, str):
                value = dt_cls.fromisoformat(value)
            elif isinstance(col.type, Boolean) and isinstance(value, int):
                value = bool(value)
        out[col.name] = value
    return out


async def _copy_table(pg_conn, table: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    sa_table = Base.metadata.tables[table]
    col_names = {c.name for c in sa_table.columns}
    payload = [
        _coerce_row(sa_table, {k: v for k, v in row.items() if k in col_names})
        for row in rows
    ]
    await pg_conn.execute(sa_table.insert(), payload)
    return len(payload)


async def _reset_sequence(pg_conn, table: str) -> None:
    await pg_conn.execute(text(
        f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
        f"COALESCE((SELECT MAX(id) FROM {table}), 1), "
        f"(SELECT MAX(id) FROM {table}) IS NOT NULL)"
    ))


async def migrate(sqlite_path: Path) -> dict[str, tuple[int, int]]:
    """Returns {table: (source_row_count, copied_row_count)}."""
    sqlite_conn = sqlite3.connect(sqlite_path)
    counts: dict[str, tuple[int, int]] = {}
    try:
        async with engine.begin() as pg_conn:
            for table in _TABLE_ORDER:
                rows = _read_sqlite_table(sqlite_conn, table)
                copied = await _copy_table(pg_conn, table, rows)
                await _reset_sequence(pg_conn, table)
                counts[table] = (len(rows), copied)
    finally:
        sqlite_conn.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite-path", default="dance.sqlite3")
    args = parser.parse_args()

    src = Path(args.sqlite_path)
    if not src.exists():
        raise SystemExit(f"source sqlite file not found: {src}")

    counts = asyncio.run(migrate(src))
    print(f"{'table':<26} {'source':>8} {'copied':>8}")
    mismatches = []
    for table, (source, copied) in counts.items():
        print(f"{table:<26} {source:>8} {copied:>8}")
        if source != copied:
            mismatches.append(table)
    if mismatches:
        raise SystemExit(f"row-count mismatch in: {', '.join(mismatches)}")


if __name__ == "__main__":
    main()
