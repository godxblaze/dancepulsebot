from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import BaseStorage
from aiogram.types import ErrorEvent
from aiogram_dialog import setup_dialogs

from app.bot.dialogs import (
    broadcast_dialog,
    create_school_dialog,
    edit_group_dialog,
    edit_name_dialog,
    new_group_dialog,
    rename_school_dialog,
)
from app.bot.handlers import (
    admin,
    attendance,
    lesson_cancel,
    pool,
    recruit,
    school,
    search,
    student,
    survey,
)
from app.bot.middlewares import ClearStaleCancelState
from app.core.config import settings

log = logging.getLogger(__name__)


async def _on_error(event: ErrorEvent) -> bool:
    """Глобальный перехват необработанных ошибок в хендлерах. Гасит «вечный
    спиннер» на callback'ах (битый/легаси callback_data, устаревший
    call.message=None у сообщений старше ~48ч) и логирует с трейсбеком."""
    log.exception("Unhandled error in update handler: %s", event.exception)
    cq = event.update.callback_query
    if cq is not None:
        try:
            await cq.answer(
                "Что-то пошло не так — возможно, сообщение устарело. "
                "Открой меню заново.",
                show_alert=True,
            )
        except Exception:  # noqa: BLE001
            pass
        return True
    message = event.update.message
    if message is not None:
        try:
            await message.answer("Что-то пошло не так. Попробуй ещё раз.")
        except Exception:  # noqa: BLE001
            pass
    return True


def build_bot() -> Bot:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is empty — set it in .env")
    return Bot(token=settings.bot_token)


def _build_storage() -> BaseStorage:
    """Persist FSM state across restarts — мастер группы и онбординг ученика
    переживают рестарт. Файл лежит рядом с основной БД."""
    from app.bot.fsm_storage import JsonFileStorage

    state_path = Path(settings.db_path).with_suffix(".fsm.json")
    return JsonFileStorage(state_path)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=_build_storage())
    # Нижнее меню — «безопасная гавань»: чистит зависший CancelLesson.reason
    # до того, как апдейт дойдёт до любого хендлера (outer — гарантированно
    # выполняется, даже если конкретный хендлер ниже не совпадёт по фильтру).
    dp.message.outer_middleware(ClearStaleCancelState())
    # student first: it claims /start with a deep-link payload
    dp.include_router(student.router)
    dp.include_router(survey.router)
    dp.include_router(recruit.router)
    dp.include_router(pool.router)
    dp.include_router(search.router)
    dp.include_router(lesson_cancel.router)
    dp.include_router(school.router)
    dp.include_router(attendance.router)
    dp.include_router(admin.router)
    # aiogram-dialog flows (NewGroup wizard и т.п.).
    dp.include_router(new_group_dialog)
    dp.include_router(edit_group_dialog)
    dp.include_router(create_school_dialog)
    dp.include_router(rename_school_dialog)
    dp.include_router(edit_name_dialog)
    dp.include_router(broadcast_dialog)
    setup_dialogs(dp)
    dp.errors.register(_on_error)
    return dp
