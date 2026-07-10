"""Tests for backup/restore — pg_dump/pg_restore invocation + pruning policy.

Retention (KEEP_MIN floor protects against a long idle gap sweeping every
copy) is tested with mocked/injected timestamps, not a real 14-day wait.
Requires `pg_dump`/`pg_restore`/`psql`/`createdb`/`dropdb` on PATH.

Backup/restore tests use their own scratch database, never the shared
`dancepulse_test` schema-per-worker database: `pg_dump` locks every table in
the *whole* target database regardless of schema/search_path, which under
concurrent xdist workers deadlocked against other workers' schema DDL.
"""
import os
import subprocess
import time
from urllib.parse import urlsplit

from scripts import backup_db, restore_db

# Host/port/user (+ password via env, never via argv) for creating scratch
# DBs — reads whatever server TEST_DATABASE_URL points at (docker-compose.test.yml,
# or the portable-Postgres trust-auth default), never the shared dancepulse_test
# database itself.
_admin_parts = urlsplit(
    backup_db.plain_url(
        os.environ.get(
            "TEST_DATABASE_URL", "postgresql+asyncpg://postgres@127.0.0.1:5432/dancepulse_test"
        )
    )
)
_PG_HOST = _admin_parts.hostname or "127.0.0.1"
_PG_PORT = _admin_parts.port or 5432
_PG_USER = _admin_parts.username or "postgres"
_PG_ENV = {"PGPASSWORD": _admin_parts.password} if _admin_parts.password else {}
_PG_ADMIN = ["-h", _PG_HOST, "-p", str(_PG_PORT), "-U", _PG_USER]
# Password embedded in the URL itself (not just passed via env) — code
# downstream of _make_scratch_db's return value (backup_db.make_backup(),
# restore_db.main()) re-derives its own PGPASSWORD via dsn_and_pg_env(url),
# which only finds a password if the DSN carries one.
_PG_AUTH = f"{_PG_USER}:{_admin_parts.password}@" if _admin_parts.password else f"{_PG_USER}@"


def test_dsn_and_pg_env_redacts_password() -> None:
    """Phase 12 security fix: пароль не должен попадать в argv (виден в
    ps/Task Manager) ни в traceback CalledProcessError — только в PGPASSWORD."""
    url, env = backup_db.dsn_and_pg_env("postgresql+asyncpg://user:s3cr3t@host:5432/db")
    assert "s3cr3t" not in url
    assert url == "postgresql://user@host:5432/db"
    assert env == {"PGPASSWORD": "s3cr3t"}


def test_dsn_and_pg_env_no_password_is_passthrough() -> None:
    url, env = backup_db.dsn_and_pg_env("postgresql+asyncpg://postgres@127.0.0.1:5432/db")
    assert url == "postgresql://postgres@127.0.0.1:5432/db"
    assert env == {}


def _touch(path, days_old: float) -> None:
    path.write_bytes(b"x")
    old = time.time() - days_old * 86400
    os.utime(path, (old, old))


def _make_scratch_db(name: str) -> str:
    env = {**os.environ, **_PG_ENV}
    subprocess.run(["dropdb", "--if-exists", *_PG_ADMIN, name], check=True, env=env)
    subprocess.run(["createdb", *_PG_ADMIN, name], check=True, env=env)
    url = f"postgresql://{_PG_AUTH}{_PG_HOST}:{_PG_PORT}/{name}"
    subprocess.run(
        ["psql", "-c",
         "CREATE TABLE probe (id serial primary key, note text); "
         "INSERT INTO probe (note) VALUES ('seed')", url],
        check=True, capture_output=True, text=True, env=env,
    )
    return url


def _drop_scratch_db(name: str) -> None:
    subprocess.run(
        ["dropdb", "--if-exists", *_PG_ADMIN, name], check=True, env={**os.environ, **_PG_ENV}
    )


def test_prune_keeps_min_recent_regardless_of_age(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(backup_db, "BACKUP_DIR", tmp_path)
    monkeypatch.setattr(backup_db, "KEEP_DAYS", 14)
    monkeypatch.setattr(backup_db, "KEEP_MIN", 3)
    # 5 копий, все старше KEEP_DAYS (20..24 дней).
    for i in range(5):
        _touch(tmp_path / f"dance-{i}.dump", days_old=20 + i)

    removed = backup_db.prune_old()
    survivors = list(tmp_path.glob("*.dump"))
    assert removed == 2
    assert len(survivors) == 3  # KEEP_MIN самых свежих уцелели


def test_prune_deletes_old_beyond_min(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(backup_db, "BACKUP_DIR", tmp_path)
    monkeypatch.setattr(backup_db, "KEEP_DAYS", 14)
    monkeypatch.setattr(backup_db, "KEEP_MIN", 3)
    for i in range(3):
        _touch(tmp_path / f"fresh-{i}.dump", days_old=1 + i)
    for i in range(2):
        _touch(tmp_path / f"old-{i}.dump", days_old=30 + i)

    removed = backup_db.prune_old()
    names = {p.name for p in tmp_path.glob("*.dump")}
    assert removed == 2
    assert all(n.startswith("fresh-") for n in names)


def test_backup_produces_valid_pg_dump_archive_and_copies_env(tmp_path, monkeypatch) -> None:
    """Периметр бэкапа: pg_dump-архив + .env попадают в backups/ рядом, с тем
    же timestamp-суффиксом. FSM-состояние с Phase 6 живёт в самой БД
    (таблица fsm_state) — уезжает вместе с pg_dump, отдельного файла нет."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(backup_db, "BACKUP_DIR", tmp_path / "backups")
    (tmp_path / ".env").write_text("BOT_TOKEN=x")

    scratch_db = f"dancepulse_backup_scratch_{os.getpid()}"
    scratch_url = _make_scratch_db(scratch_db)
    monkeypatch.setattr(
        backup_db.settings, "database_url", scratch_url.replace("postgresql://", "postgresql+asyncpg://")
    )
    try:
        dst = backup_db.make_backup()
        assert dst.exists() and dst.stat().st_size > 0
        # pg_dump custom-format (-Fc) archives start with this 5-byte magic.
        assert dst.read_bytes()[:5] == b"PGDMP"
        names = {p.name for p in backup_db.BACKUP_DIR.glob("*")}
        assert any(n.startswith("env-") and n.endswith(".bak") for n in names)
    finally:
        _drop_scratch_db(scratch_db)


def test_prune_env_respects_keep_min(tmp_path, monkeypatch) -> None:
    """.env-бэкапы прунятся тем же KEEP_MIN-правилом, что и dump."""
    monkeypatch.setattr(backup_db, "BACKUP_DIR", tmp_path)
    monkeypatch.setattr(backup_db, "KEEP_DAYS", 14)
    monkeypatch.setattr(backup_db, "KEEP_MIN", 2)
    for i in range(4):
        _touch(tmp_path / f"env-{i}.bak", days_old=20 + i)

    removed = backup_db.prune_old()
    assert removed == 2
    assert len(list(tmp_path.glob("env-*.bak"))) == 2


def test_restore_round_trip_into_scratch_database(tmp_path, monkeypatch) -> None:
    """backup_db.make_backup() → restore_db.main() в другую scratch-БД →
    запрос по core-таблице проходит — доказывает механизм pg_restore
    end-to-end. Источник и цель — обе одноразовые scratch-БД, не shared
    dancepulse_test."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(backup_db, "BACKUP_DIR", tmp_path / "backups")

    source_db = f"dancepulse_backup_scratch_{os.getpid()}"
    source_url = _make_scratch_db(source_db)
    monkeypatch.setattr(
        backup_db.settings, "database_url", source_url.replace("postgresql://", "postgresql+asyncpg://")
    )

    target_db = f"dancepulse_restore_scratch_{os.getpid()}"
    try:
        dst = backup_db.make_backup()
        env = {**os.environ, **_PG_ENV}
        subprocess.run(["dropdb", "--if-exists", *_PG_ADMIN, target_db], check=True, env=env)
        subprocess.run(["createdb", *_PG_ADMIN, target_db], check=True, env=env)
        target_url = f"postgresql://{_PG_AUTH}{_PG_HOST}:{_PG_PORT}/{target_db}"

        restore_db.main(str(dst), target_url=target_url, sanity_table="probe")
        count = restore_db._row_count(target_url, "probe", _PG_ENV)
        assert count == 1
    finally:
        _drop_scratch_db(source_db)
        _drop_scratch_db(target_db)
