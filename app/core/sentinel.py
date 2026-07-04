from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path

CONFLICT_MARK = "TelegramConflictError"
DEDUP_WINDOW = timedelta(hours=1)
TAIL_LINES = 200


class ConflictSentinel:
    """Читает хвост лог-файла на TelegramConflictError (getUpdates conflict —
    второй экземпляр бота делает polling тем же токеном) и шлёт один алерт,
    дедупя повторы в течение dedup_window. aiogram 3.27 в _listen_updates
    ретраит эту ошибку бесконечно, поэтому она никогда не всплывает как
    исключение polling-таска — только через лог."""

    def __init__(
        self,
        log_path: Path,
        *,
        dedup_window: timedelta = DEDUP_WINDOW,
        tail_lines: int = TAIL_LINES,
    ) -> None:
        self.log_path = log_path
        self.dedup_window = dedup_window
        self.tail_lines = tail_lines
        self.last_alert_at: datetime | None = None

    def _tail(self) -> list[str]:
        if not self.log_path.exists():
            return []
        with self.log_path.open("r", encoding="utf-8", errors="ignore") as f:
            return f.readlines()[-self.tail_lines :]

    async def check(self, now: datetime, alert: Callable[[str], Awaitable[None]]) -> bool:
        """Возвращает True, если алерт был отправлен (для тестов)."""
        if not any(CONFLICT_MARK in line for line in self._tail()):
            return False
        if self.last_alert_at is not None and now - self.last_alert_at < self.dedup_window:
            return False
        await alert("⚠️ Похоже, запущен второй экземпляр бота (getUpdates conflict)")
        self.last_alert_at = now
        return True
