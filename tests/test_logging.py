"""app/core/logging.py — пункт #20 аудита (модуль без тестов). setup_logging
мутирует root-логгер глобально, поэтому каждый тест сохраняет и восстанавливает
исходные handlers/level, чтобы не сломать логирование других тестов процесса."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import app.core.logging as app_logging
from app.core.logging import setup_logging


def _snapshot_root() -> tuple[list[logging.Handler], int]:
    root = logging.getLogger()
    return list(root.handlers), root.level


def _restore_root(snapshot: tuple[list[logging.Handler], int]) -> None:
    handlers, level = snapshot
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)


def test_setup_logging_creates_log_dir_and_two_handlers(tmp_path, monkeypatch) -> None:
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(app_logging, "LOG_DIR", log_dir)
    monkeypatch.setattr(app_logging, "LOG_FILE", log_dir / "dance.log")
    snapshot = _snapshot_root()
    try:
        setup_logging()
        assert log_dir.is_dir()
        root = logging.getLogger()
        assert len(root.handlers) == 2
        kinds = {type(h) for h in root.handlers}
        assert logging.StreamHandler in kinds
        assert RotatingFileHandler in kinds
    finally:
        _restore_root(snapshot)


def test_setup_logging_sets_level_from_settings(tmp_path, monkeypatch) -> None:
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(app_logging, "LOG_DIR", log_dir)
    monkeypatch.setattr(app_logging, "LOG_FILE", log_dir / "dance.log")
    monkeypatch.setattr(app_logging.settings, "log_level", "WARNING")
    snapshot = _snapshot_root()
    try:
        setup_logging()
        assert logging.getLogger().level == logging.WARNING
    finally:
        _restore_root(snapshot)


def test_setup_logging_is_idempotent_not_additive(tmp_path, monkeypatch) -> None:
    """Повторный вызов (рестарт логирования) не должен копить дублирующиеся
    handlers — root.handlers.clear() в начале обязателен."""
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(app_logging, "LOG_DIR", log_dir)
    monkeypatch.setattr(app_logging, "LOG_FILE", log_dir / "dance.log")
    snapshot = _snapshot_root()
    try:
        setup_logging()
        setup_logging()
        assert len(logging.getLogger().handlers) == 2
    finally:
        _restore_root(snapshot)


def test_setup_logging_file_handler_rotation_config(tmp_path, monkeypatch) -> None:
    log_dir = tmp_path / "logs"
    log_file = log_dir / "dance.log"
    monkeypatch.setattr(app_logging, "LOG_DIR", log_dir)
    monkeypatch.setattr(app_logging, "LOG_FILE", log_file)
    snapshot = _snapshot_root()
    try:
        setup_logging()
        file_handler = next(
            h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)
        )
        assert file_handler.maxBytes == 5 * 1024 * 1024
        assert file_handler.backupCount == 5
        assert Path(file_handler.baseFilename) == log_file.resolve()
    finally:
        _restore_root(snapshot)
