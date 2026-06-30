"""Tests for backup pruning — KEEP_MIN floor protects against a long idle gap
sweeping every copy."""
import os
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
