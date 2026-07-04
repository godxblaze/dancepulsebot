"""Tests for backup pruning — KEEP_MIN floor protects against a long idle gap
sweeping every copy."""
import os
import sqlite3
import time

from scripts import backup_db


def _touch(path, days_old: float) -> None:
    path.write_bytes(b"x")
    old = time.time() - days_old * 86400
    os.utime(path, (old, old))


def test_prune_keeps_min_recent_regardless_of_age(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(backup_db, "BACKUP_DIR", tmp_path)
    monkeypatch.setattr(backup_db, "KEEP_DAYS", 14)
    monkeypatch.setattr(backup_db, "KEEP_MIN", 3)
    # 5 копий, все старше KEEP_DAYS (20..24 дней).
    for i in range(5):
        _touch(tmp_path / f"dance-{i}.sqlite3", days_old=20 + i)

    removed = backup_db.prune_old()
    survivors = list(tmp_path.glob("*.sqlite3"))
    assert removed == 2
    assert len(survivors) == 3  # KEEP_MIN самых свежих уцелели


def test_prune_deletes_old_beyond_min(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(backup_db, "BACKUP_DIR", tmp_path)
    monkeypatch.setattr(backup_db, "KEEP_DAYS", 14)
    monkeypatch.setattr(backup_db, "KEEP_MIN", 3)
    for i in range(3):
        _touch(tmp_path / f"fresh-{i}.sqlite3", days_old=1 + i)
    for i in range(2):
        _touch(tmp_path / f"old-{i}.sqlite3", days_old=30 + i)

    removed = backup_db.prune_old()
    names = {p.name for p in tmp_path.glob("*.sqlite3")}
    assert removed == 2
    assert all(n.startswith("fresh-") for n in names)


def test_backup_copies_env_and_fsm_state(tmp_path, monkeypatch) -> None:
    """Периметр бэкапа: .env и <db>.fsm.json попадают в backups/ рядом со
    снапшотом БД, с тем же timestamp-суффиксом."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(backup_db, "BACKUP_DIR", tmp_path / "backups")
    db_path = tmp_path / "dance.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(backup_db.settings, "db_path", str(db_path))
    (tmp_path / ".env").write_text("BOT_TOKEN=x")
    (tmp_path / "dance.fsm.json").write_text("{}")

    backup_db.make_backup()

    names = {p.name for p in backup_db.BACKUP_DIR.glob("*")}
    assert any(n.startswith("env-") and n.endswith(".bak") for n in names)
    assert any(n.endswith(".fsm.json") for n in names)


def test_prune_env_and_fsm_respects_keep_min(tmp_path, monkeypatch) -> None:
    """.env/.fsm.json бэкапы прунятся тем же KEEP_MIN-правилом, что и sqlite3."""
    monkeypatch.setattr(backup_db, "BACKUP_DIR", tmp_path)
    monkeypatch.setattr(backup_db, "KEEP_DAYS", 14)
    monkeypatch.setattr(backup_db, "KEEP_MIN", 2)
    for i in range(4):
        _touch(tmp_path / f"env-{i}.bak", days_old=20 + i)
        _touch(tmp_path / f"dance-{i}.fsm.json", days_old=20 + i)

    removed = backup_db.prune_old()
    assert removed == 4  # 2 старше KEEP_MIN в каждой из двух серий
    assert len(list(tmp_path.glob("env-*.bak"))) == 2
    assert len(list(tmp_path.glob("*.fsm.json"))) == 2
