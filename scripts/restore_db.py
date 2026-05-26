"""Восстановить SQLite-базу из бэкапа.

Использование:
    python -m scripts.restore_db <path-to-backup.sqlite3>

Перед запуском бот должен быть остановлен (иначе WAL-файл может прийти
в несогласованное состояние). Скрипт:
1. Делает страховочный snapshot текущей базы (если она есть) в backups/.
2. Удаляет dance.sqlite3 + WAL/shm.
3. Копирует указанный backup-файл в dance.sqlite3.
4. PRAGMA integrity_check на новой базе — упасть здесь лучше, чем потом
   получать странные ошибки при работе.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from app.core.config import settings


def main(backup_path: str) -> None:
    backup = Path(backup_path)
    if not backup.exists():
        print(f"backup не найден: {backup}", file=sys.stderr)
        sys.exit(1)

    target = Path(settings.db_path)
    backups_dir = Path("backups")
    backups_dir.mkdir(exist_ok=True)

    # 1. safety snapshot
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safety = backups_dir / f"{target.stem}-pre-restore-{stamp}.sqlite3"
        with sqlite3.connect(target) as src_conn, sqlite3.connect(safety) as dst_conn:
            src_conn.backup(dst_conn)
        print(f"safety snapshot: {safety}")

    # 2. wipe current db + WAL/shm
    for ext in ("", "-wal", "-shm", "-journal"):
        p = Path(str(target) + ext)
        if p.exists():
            p.unlink()
            print(f"removed: {p}")

    # 3. copy backup
    shutil.copy2(backup, target)
    print(f"restored: {backup} → {target}")

    # 4. integrity check
    with sqlite3.connect(target) as conn:
        result = conn.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        print(f"⚠ integrity_check вернул: {result}", file=sys.stderr)
        sys.exit(2)
    print("integrity ok — можно стартовать бота.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])
