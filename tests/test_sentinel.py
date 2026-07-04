"""ConflictSentinel — детект TelegramConflictError в хвосте лога, с дедупом
алертов не чаще раза в час."""
from datetime import datetime, timedelta

from app.core.sentinel import ConflictSentinel


class _Alerts:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, text: str) -> None:
        self.sent.append(text)


async def test_conflict_in_log_triggers_one_alert(tmp_path) -> None:
    log_path = tmp_path / "dance.log"
    log_path.write_text("2026-07-04 ERROR dance.main | TelegramConflictError: conflict\n")
    sentinel = ConflictSentinel(log_path)
    alerts = _Alerts()

    fired = await sentinel.check(datetime(2026, 7, 4, 12, 0), alerts)

    assert fired is True
    assert len(alerts.sent) == 1


async def test_repeat_within_dedup_window_does_not_alert(tmp_path) -> None:
    log_path = tmp_path / "dance.log"
    log_path.write_text("TelegramConflictError: conflict\n")
    sentinel = ConflictSentinel(log_path, dedup_window=timedelta(hours=1))
    alerts = _Alerts()

    await sentinel.check(datetime(2026, 7, 4, 12, 0), alerts)
    fired_again = await sentinel.check(datetime(2026, 7, 4, 12, 30), alerts)

    assert fired_again is False
    assert len(alerts.sent) == 1


async def test_clean_log_does_not_alert(tmp_path) -> None:
    log_path = tmp_path / "dance.log"
    log_path.write_text("2026-07-04 INFO dance.main | Bot polling started\n")
    sentinel = ConflictSentinel(log_path)
    alerts = _Alerts()

    fired = await sentinel.check(datetime(2026, 7, 4, 12, 0), alerts)

    assert fired is False
    assert alerts.sent == []


async def test_missing_log_file_does_not_alert(tmp_path) -> None:
    sentinel = ConflictSentinel(tmp_path / "nonexistent.log")
    alerts = _Alerts()

    fired = await sentinel.check(datetime(2026, 7, 4, 12, 0), alerts)

    assert fired is False
    assert alerts.sent == []
