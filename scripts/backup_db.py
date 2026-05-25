"""Атомарный snapshot SQLite-базы через sqlite3.backup() — безопасно на живом боте.

Запуск:
    python -m scripts.backup_db

Кладёт файл в backups/dance-YYYYMMDD-HHMMSS.sqlite3 и удаляет копии старше KEEP_DAYS.
Подключать через Windows Task Scheduler / cron — раз в сутки.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from app.core.config import settings

KEEP_DAYS = 14
BACKUP_DIR = Path("backups")


def make_backup() -> Path:
    src = Path(settings.db_path)
    if not src.exists():
        print(f"source db not found: {src}", file=sys.stderr)
        sys.exit(1)

    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = BACKUP_DIR / f"{src.stem}-{stamp}.sqlite3"

    with sqlite3.connect(src) as src_conn, sqlite3.connect(dst) as dst_conn:
        src_conn.backup(dst_conn)
    return dst


def prune_old() -> int:
    cutoff = datetime.now() - timedelta(days=KEEP_DAYS)
    removed = 0
    for p in BACKUP_DIR.glob("*.sqlite3"):
        if datetime.fromtimestamp(p.stat().st_mtime) < cutoff:
            p.unlink()
            removed += 1
    return removed


def main() -> None:
    dst = make_backup()
    size_kb = dst.stat().st_size // 1024
    pruned = prune_old()
    print(f"backup: {dst} ({size_kb} KB); pruned {pruned} old file(s)")


if __name__ == "__main__":
    main()
