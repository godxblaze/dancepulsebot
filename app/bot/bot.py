from __future__ import annotations

from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import BaseStorage
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
    student,
    survey,
)
from app.core.config import settings


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
    # student first: it claims /start with a deep-link payload
    dp.include_router(student.router)
    dp.include_router(survey.router)
    dp.include_router(recruit.router)
    dp.include_router(pool.router)
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
    return dp
