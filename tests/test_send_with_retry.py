"""429/TelegramRetryAfter — send_with_retry не должен тихо терять сообщение
рассылки на первом же rate-limit'е (пилот-аудит: рассылки без троттлинга)."""
from types import SimpleNamespace

from aiogram.exceptions import TelegramRetryAfter

from app.bot.utils import send_with_retry


class _FakeBotRetryOnce:
    """bot.send_message падает TelegramRetryAfter один раз, потом отдаёт успех."""

    def __init__(self, retry_after: float = 0.01) -> None:
        self.retry_after = retry_after
        self.calls: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> str:
        self.calls.append((chat_id, text))
        if len(self.calls) == 1:
            raise TelegramRetryAfter(
                SimpleNamespace(chat_id=chat_id), "flood", self.retry_after
            )
        return "sent"


class _FakeBotAlwaysRetry:
    """Второй 429 подряд — send_with_retry не зацикливается, пробрасывает."""

    async def send_message(self, chat_id: int, text: str, **kwargs) -> str:
        raise TelegramRetryAfter(SimpleNamespace(chat_id=chat_id), "flood", 0.01)


async def test_send_with_retry_retries_once_after_429() -> None:
    bot = _FakeBotRetryOnce()
    result = await send_with_retry(bot, 42, "hello")
    assert result == "sent"
    assert len(bot.calls) == 2  # первый упал 429'ом, второй — успех


async def test_send_with_retry_propagates_repeated_429() -> None:
    bot = _FakeBotAlwaysRetry()
    try:
        await send_with_retry(bot, 42, "hello")
    except TelegramRetryAfter:
        pass
    else:
        raise AssertionError("expected TelegramRetryAfter to propagate")
