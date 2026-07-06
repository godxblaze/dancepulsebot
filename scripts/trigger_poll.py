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
from app.services import cancellations as cancellations_svc
from app.services import groups as groups_svc
from app.services import schools as schools_svc
from app.services import surveys as surveys_svc
from app.services.schedule import format_lesson_at, next_lesson_at, utc_now

log = logging.getLogger("trigger")


async def run() -> None:
    setup_logging()
    await init_db()
    bot = build_bot()
    now = utc_now()  # scheduler convention: aware UTC
    try:
        async with session_scope() as s:
            groups = await groups_svc.active_groups(s)
        if not groups:
            print("Нет активных групп — сначала создай группу через /newgroup.")
            return
        for group in groups:
            tz = None
            if group.school_id is not None:
                async with session_scope() as s:
                    tz = await schools_svc.get_timezone(s, group.school_id)
            lesson_at = next_lesson_at(group.weekdays, group.lesson_time, now, tz=tz)
            if lesson_at is None:
                print(f"«{group.name}»: не заданы дни недели — пропуск.")
                continue
            when = format_lesson_at(lesson_at, "%d.%m %H:%M", tz=tz)
            async with session_scope() as s:
                if await cancellations_svc.is_cancelled(s, group.id, lesson_at):
                    print(f"«{group.name}»: занятие {when} отменено — пропуск.")
                    continue
                existing = await surveys_svc.find_attendance_survey(
                    s, group.id, lesson_at
                )
            if existing is not None:
                print(f"«{group.name}»: опрос на {when} уже разослан.")
                continue
            print(f"«{group.name}»: рассылаю опрос на {when}...")
            await _poll_group(bot, group, lesson_at, now, tz=tz)
    finally:
        await bot.session.close()
    print("Готово.")


if __name__ == "__main__":
    asyncio.run(run())
