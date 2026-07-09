"""Snapshot Postgres через `pg_dump -Fc` — custom-format архив, безопасно на
живом боте (pg_dump берёт консистентный snapshot средствами MVCC, не блокирует
читателей/писателей).

Запуск:
    python -m scripts.backup_db

Кладёт архив в backups/dance-YYYYMMDD-HHMMSS.dump (+ копию .env с тем же
timestamp-суффиксом, если он существует) и удаляет копии старше KEEP_DAYS.
Подключать через Windows Task Scheduler / cron — раз в сутки.

FSM-состояние (диалоги/онбординг) с Phase 6 живёт в таблице `fsm_state` той
же Postgres-базы — уезжает вместе с ней в pg_dump, отдельного бэкапа не
требует (см. `dance.fsm.json` decommission decision в app/bot/fsm_storage.py).

Требует `pg_dump` на PATH (боевой деплой — пакет postgresql-client).

Секреты (.env с токеном бота) остаются в локальном backups/ — offsite-копия
только в приватное хранилище, см. docs/PILOT_DAY0_CHECKLIST.md.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from app.core.config import settings

KEEP_DAYS = 14
# Всегда держим минимум столько самых свежих копий — даже если все они старше
# KEEP_DAYS (напр. бот простоял дольше двух недель). Иначе долгий простой
# подчистую сметает всё, кроме последней.
KEEP_MIN = 7
BACKUP_DIR = Path("backups")


def plain_url(database_url: str) -> str:
    """pg_dump/pg_restore/psql говорят на libpq URI — убираем SQLAlchemy-суффикс
    `+asyncpg` из DATABASE_URL."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def dsn_and_pg_env(database_url: str) -> tuple[str, dict[str, str]]:
    """Вынимает пароль из DSN в `PGPASSWORD`-переменную окружения дочернего
    процесса — иначе пароль виден в argv (`ps`/Task Manager) любому на
    машине, а при `subprocess.CalledProcessError` (check=True) целиком
    попадает в traceback/лог (`.cmd` печатается как есть)."""
    url = plain_url(database_url)
    parts = urlsplit(url)
    if parts.password is None:
        return url, {}
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    if parts.username:
        netloc = f"{parts.username}@{netloc}"
    redacted = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return redacted, {"PGPASSWORD": parts.password}


def make_backup() -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = BACKUP_DIR / f"dance-{stamp}.dump"

    url, pg_env = dsn_and_pg_env(settings.database_url)
    subprocess.run(
        ["pg_dump", "-Fc", "-f", str(dst), url],
        check=True, env={**os.environ, **pg_env},
    )

    env = Path(".env")
    if env.exists():
        shutil.copy2(env, BACKUP_DIR / f"env-{stamp}.bak")
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
    """БД и .env-снапшоты создаются в одном make_backup() с одинаковым mtime,
    поэтому прунинг каждой серии тем же правилом (KEEP_MIN свежих
    неприкосновенны) держит их в шаге друг с другом."""
    return _prune_glob("*.dump") + _prune_glob("env-*.bak")


def main() -> None:
    dst = make_backup()
    size_kb = dst.stat().st_size // 1024
    pruned = prune_old()
    print(f"backup: {dst} ({size_kb} KB); pruned {pruned} old file(s)")


if __name__ == "__main__":
    main()
