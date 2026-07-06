"""_on_error раньше отвечал пользователю только на callback-ветке — упавший
message-хендлер (например /stats) молчал, хотя ошибка логировалась."""
from types import SimpleNamespace

from app.bot.bot import _on_error


class _FakeAnswerable:
    def __init__(self) -> None:
        self.answered_with: list[tuple] = []

    async def answer(self, *args, **kwargs) -> None:
        self.answered_with.append((args, kwargs))


async def test_on_error_replies_to_callback_query() -> None:
    cq = _FakeAnswerable()
    event = SimpleNamespace(
        exception=RuntimeError("boom"),
        update=SimpleNamespace(callback_query=cq, message=None),
    )
    result = await _on_error(event)
    assert result is True
    assert cq.answered_with
    assert cq.answered_with[0][1]["show_alert"] is True


async def test_on_error_replies_to_message() -> None:
    msg = _FakeAnswerable()
    event = SimpleNamespace(
        exception=RuntimeError("boom"),
        update=SimpleNamespace(callback_query=None, message=msg),
    )
    result = await _on_error(event)
    assert result is True
    assert msg.answered_with  # раньше здесь было пусто — пользователь не узнавал


async def test_on_error_survives_double_failure() -> None:
    """Даже если сам answer() тоже упадёт — обработчик не должен пробрасывать."""
    class _Broken:
        async def answer(self, *args, **kwargs) -> None:
            raise RuntimeError("telegram недоступен")

    event = SimpleNamespace(
        exception=RuntimeError("boom"),
        update=SimpleNamespace(callback_query=None, message=_Broken()),
    )
    result = await _on_error(event)
    assert result is True
