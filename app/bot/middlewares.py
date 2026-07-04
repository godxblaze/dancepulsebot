"""Bot-level middlewares — cross-cutting concerns not owned by one handler module."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message

from app.bot.keyboards import MENU_TEXTS
from app.bot.states import CancelLesson


class ClearStaleCancelState(BaseMiddleware):
    """Нижнее меню — «безопасная гавань»: тап любой menu-кнопки при зависшем
    CancelLesson.reason (напр. открыл «Другое» на confirm-экране отмены и ушёл
    в другой раздел) чистит state. Иначе случайный текст, набранный позже в
    несвязанном контексте, рискует быть принят за причину отмены."""

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if event.text in MENU_TEXTS:
            state = data.get("state")
            if state is not None and await state.get_state() == CancelLesson.reason.state:
                await state.clear()
        return await handler(event, data)
