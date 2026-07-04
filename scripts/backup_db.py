"""Атомарный snapshot SQLite-базы через sqlite3.backup() — безопасно на живом боте.

Запуск:
    python -m scripts.backup_db

Кладёт файл в backups/dance-YYYYMMDD-HHMMSS.sqlite3 (+ копии .env и *.fsm.json
с тем же timestamp-суффиксом, если они существуют) и удаляет копии старше
KEEP_DAYS. Подключать через Windows Task Scheduler / cron — раз в сутки.

Секреты (.env с токеном бота) остаются в локальном backups/ — offsite-копия
только в приватное хранилище, см. docs/PILOT_DAY0_CHECKLIST.md.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from app.core.config import settings

KEEP_DAYS = 14
# Всегда держим минимум столько самых свежих копий — даже если все они старше
# KEEP_DAYS (напр. бот простоял дольше двух недель). Иначе долгий простой
# подчистую сметает всё, кроме последней.
KEEP_MIN = 7
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

    env = Path(".env")
    if env.exists():
        shutil.copy2(env, BACKUP_DIR / f"env-{stamp}.bak")
    fsm = src.with_suffix(".fsm.json")
    if fsm.exists():
        shutil.copy2(fsm, BACKUP_DIR / f"{src.stem}-{stamp}.fsm.json")
    return dst


def _prune_glob(pattern: str) -> int:
    cutoff = datetime.now() - timedelta(days=KEEP_DAYS)
    # Сначала самые свежие; первые KEEP_MIN не трогаем независимо от возраста.
    files = sorted(
        BACKUP_DIR.glob(pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for p in files[KEEP_MIN:]:
        if datetime.fromtimestamp(p.stat().st_mtime) < cutoff:
            p.unlink()
            removed += 1
    return removed


def prune_old() -> int:
    """БД, .env и fsm-снапшоты создаются в одном make_backup() с одинаковым
    mtime, поэтому прунинг каждой серии тем же правилом (KEEP_MIN свежих
    неприкосновенны) держит их в шаге друг с другом."""
    return (
        _prune_glob("*.sqlite3")
        + _prune_glob("env-*.bak")
        + _prune_glob("*.fsm.json")
    )


def main() -> None:
    dst = make_backup()
    size_kb = dst.stat().st_size // 1024
    pruned = prune_old()
    print(f"backup: {dst} ({size_kb} KB); pruned {pruned} old file(s)")


if __name__ == "__main__":
    main()
