from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import recruit, student, survey, teacher
from app.core.config import settings


def build_bot() -> Bot:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is empty — set it in .env")
    return Bot(token=settings.bot_token)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    # student first: it claims /start with a deep-link payload
    dp.include_router(student.router)
    dp.include_router(survey.router)
    dp.include_router(recruit.router)
    dp.include_router(teacher.router)
    return dp
