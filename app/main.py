from __future__ import annotations

import asyncio
import logging

from aiogram.types import BotCommand

from app.bot.bot import build_bot, build_dispatcher
from app.core.logging import setup_logging
from app.db.session import init_db
from app.scheduler.scheduler import build_scheduler

log = logging.getLogger("dance.main")

_COMMANDS = [
    BotCommand(command="newgroup", description="Создать группу"),
    BotCommand(command="groups", description="Мои группы"),
    BotCommand(command="today", description="Сводка на сегодня"),
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="pool", description="Пул саппортов"),
    BotCommand(command="help", description="Справка"),
]


async def run() -> None:
    setup_logging()
    await init_db()
    log.info("Database ready")

    bot = build_bot()
    await bot.set_my_commands(_COMMANDS)
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
