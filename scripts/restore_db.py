"""Восстановить Postgres-базу из pg_dump-архива (custom format, `-Fc`).

Использование:
    python -m scripts.restore_db <path-to-backup.dump> [--target-url <DSN>]

Без --target-url восстанавливает поверх settings.database_url — той же БД, что
использует бот. **Останови бота перед восстановлением поверх боевой БД.**

Скрипт:
1. Делает страховочный pg_dump текущего состояния target-БД в backups/.
2. `pg_restore --clean --if-exists` в target-БД (--clean дропает существующие
   объекты перед восстановлением, --if-exists молчит если их и не было).
3. Sanity-check: запрос по core-таблице вместо SQLite-специфичного
   `PRAGMA integrity_check` — у Postgres прямого аналога нет; успешный
   exit-код pg_restore плюс базовый запрос достаточно ловят битый архив или
   несовместимую схему.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from app.core.config import settings
from scripts.backup_db import dsn_and_pg_env

BACKUP_DIR = Path("backups")


def _row_count(url: str, table: str, env: dict[str, str] | None = None) -> int:
    # Flags must precede the positional DSN — psql stops option parsing after
    # the first positional argument, silently ignoring anything after it.
    result = subprocess.run(
        ["psql", "-tAc", f"SELECT COUNT(*) FROM {table}", url],
        check=True, capture_output=True, text=True, env={**os.environ, **(env or {})},
    )
    return int(result.stdout.strip())


def main(backup_path: str, target_url: str | None = None, sanity_table: str = "admins") -> None:
    backup = Path(backup_path)
    if not backup.exists():
        print(f"backup не найден: {backup}", file=sys.stderr)
        sys.exit(1)

    url, pg_env = dsn_and_pg_env(target_url or settings.database_url)
    env = {**os.environ, **pg_env}
    BACKUP_DIR.mkdir(exist_ok=True)

    # 1. safety snapshot текущего состояния target'а (пустая БД — тоже валидный
    #    крошечный архив, pg_dump тут не падает).
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safety = BACKUP_DIR / f"dance-pre-restore-{stamp}.dump"
    subprocess.run(["pg_dump", "-Fc", "-f", str(safety), url], check=True, env=env)
    print(f"safety snapshot: {safety}")

    # 2. restore
    subprocess.run(
        ["pg_restore", "--clean", "--if-exists", "-d", url, str(backup)],
        check=True, env=env,
    )
    print(f"restored: {backup} → {url}")

    # 3. sanity check
    count = _row_count(url, sanity_table, pg_env)
    print(f"sanity check: {count} row(s) в {sanity_table} — restore ok, можно стартовать бота.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup_path")
    parser.add_argument("--target-url", default=None)
    parser.add_argument(
        "--sanity-table", default="admins",
        help="Таблица для пост-restore sanity-check (по умолчанию admins)",
    )
    args = parser.parse_args()
    main(args.backup_path, args.target_url, args.sanity_table)
