"""validate_runtime() должен ловить типичные опечатки в .env на старте, а не
падать загадочно вглубине polling-loop (TIMEZONE) или setup_logging (LOG_LEVEL)."""
from app.core.config import Settings


def _settings(**overrides) -> Settings:
    base = dict(bot_token="123456:" + "a" * 20, developer_id=1, default_lead_hours=18)
    base.update(overrides)
    return Settings(**base)


def test_validate_runtime_accepts_defaults() -> None:
    _settings().validate_runtime()  # не должно бросать


def test_validate_runtime_rejects_bad_timezone() -> None:
    s = _settings(timezone="Mars/Olympus")
    try:
        s.validate_runtime()
        raise AssertionError("должно было бросить")
    except RuntimeError as e:
        assert "TIMEZONE" in str(e)


def test_validate_runtime_rejects_bad_log_level() -> None:
    s = _settings(log_level="verbose")
    try:
        s.validate_runtime()
        raise AssertionError("должно было бросить")
    except RuntimeError as e:
        assert "LOG_LEVEL" in str(e)


def test_validate_runtime_accepts_lowercase_log_level() -> None:
    _settings(log_level="info").validate_runtime()  # регистр не важен
