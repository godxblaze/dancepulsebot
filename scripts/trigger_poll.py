"""Manually fire the attendance poll for all active groups — for testing
without waiting for the lead-hours window.

Usage: python -m scripts.trigger_poll
"""
from __future__ import annotations

import asyncio
import logging

from app.bot.bot import build_bot
from app.core.logging import setup_logging
from app.db.session import init_db, session_scope
from app.scheduler.scheduler import _poll_group
from app.services import groups as groups_svc
from app.services import surveys as surveys_svc
from app.services.schedule import local_now, next_lesson_at

log = logging.getLogger("trigger")


async def run() -> None:
    setup_logging()
    await init_db()
    bot = build_bot()
    now = local_now()
    try:
        async with session_scope() as s:
            groups = await groups_svc.active_groups(s)
        if not groups:
            print("Нет активных групп — сначала создай группу через /newgroup.")
            return
        for group in groups:
            lesson_at = next_lesson_at(group.weekdays, group.lesson_time, now)
            if lesson_at is None:
                print(f"«{group.name}»: не заданы дни недели — пропуск.")
                continue
            async with session_scope() as s:
                existing = await surveys_svc.find_attendance_survey(
                    s, group.id, lesson_at
                )
            if existing is not None:
                print(f"«{group.name}»: опрос на {lesson_at:%d.%m %H:%M} уже разослан.")
                continue
            print(f"«{group.name}»: рассылаю опрос на {lesson_at:%d.%m %H:%M}...")
            await _poll_group(bot, group, lesson_at)
    finally:
        await bot.session.close()
    print("Готово.")


if __name__ == "__main__":
    asyncio.run(run())
