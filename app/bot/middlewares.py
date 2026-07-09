"""Bot-level middlewares — cross-cutting concerns not owned by one handler module."""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import MENU_TEXTS
from app.bot.states import CancelLesson, SetLessonPlace
from app.core.metrics import RollingStats

# Текстовые FSM-шаги, которые не должны переживать уход в другой раздел.
_STALE_TEXT_STATES = {CancelLesson.reason.state, SetLessonPlace.place.state}

# Phase 9 observability — живой gauge времени обработки callback_query
# end-to-end (весь хендлер, включая его собственный call.answer()).
_callback_latency = RollingStats()


def callback_latency_stats() -> dict:
    return _callback_latency.snapshot()


class CallbackLatencyMiddleware(BaseMiddleware):
    """Тайминг для devstats — оборачивает весь путь обработки callback_query."""

    async def __call__(
        self,
        handler: Callable[[CallbackQuery, dict[str, Any]], Awaitable[Any]],
        event: CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        start = time.monotonic()
        try:
            return await handler(event, data)
        finally:
            _callback_latency.record(time.monotonic() - start)


class ClearStaleCancelState(BaseMiddleware):
    """Нижнее меню — «безопасная гавань»: тап любой menu-кнопки при зависшем
    текстовом шаге (причина отмены, место занятия — открыл и ушёл в другой
    раздел) чистит state. Иначе случайный текст, набранный позже в несвязанном
    контексте, рискует быть принят за причину отмены / место."""

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        if event.text in MENU_TEXTS:
            state = data.get("state")
            if state is not None and await state.get_state() in _STALE_TEXT_STATES:
                await state.clear()
        return await handler(event, data)
