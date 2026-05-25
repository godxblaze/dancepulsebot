from __future__ import annotations

import asyncio
import logging

from app.bot.bot import build_bot, build_dispatcher
from app.bot.commands import grant_teacher_commands, set_default_commands
from app.core.config import settings
from app.core.logging import setup_logging
from app.db.session import init_db, session_scope
from app.scheduler.scheduler import build_scheduler
from app.services import teachers as teachers_svc

log = logging.getLogger("dance.main")


async def run() -> None:
    setup_logging()
    settings.validate_runtime()
    await init_db()
    log.info("Database ready")

    bot = build_bot()
    await set_default_commands(bot)
    async with session_scope() as s:
        for t in await teachers_svc.list_all(s):
            try:
                await grant_teacher_commands(bot, t.tg_user_id)
            except Exception as e:  # noqa: BLE001
                log.warning("Could not set teacher commands for %s: %s", t.tg_user_id, e)
    dp = build_dispatcher()
    scheduler = build_scheduler(bot)
    scheduler.start()
    log.info("Bot polling started")

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        log.info("Shutdown complete")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
