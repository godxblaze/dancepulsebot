from __future__ import annotations

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

STUDENT_COMMANDS = [
    BotCommand(command="start", description="Начать"),
    BotCommand(command="groups", description="Мои группы"),
    BotCommand(command="today", description="Сегодня"),
    BotCommand(command="name", description="Изменить моё имя"),
]

ADMIN_COMMANDS = [
    BotCommand(command="newgroup", description="Создать группу"),
    BotCommand(command="groups", description="Мои группы"),
    BotCommand(command="today", description="Сводка на сегодня"),
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="pool", description="Пул саппортов"),
    BotCommand(command="export", description="Экспорт в CSV"),
    BotCommand(command="name", description="Изменить моё имя"),
    BotCommand(command="help", description="Справка"),
]


async def set_default_commands(bot: Bot) -> None:
    await bot.set_my_commands(STUDENT_COMMANDS, scope=BotCommandScopeDefault())


async def grant_admin_commands(bot: Bot, tg_user_id: int) -> None:
    await bot.set_my_commands(
        ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=tg_user_id)
    )


async def revoke_admin_commands(bot: Bot, tg_user_id: int) -> None:
    """Drop the per-chat admin menu — user falls back to the default (student) list."""
    await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=tg_user_id))


_BOT_DESCRIPTION = (
    "Dance Pulse — бот посещаемости для танцевальных школ.\n\n"
    "Перед каждым занятием спрашивает учеников «придёшь?» и присылает админу "
    "готовую сводку. Когда в группе перекос пар по полу — сам зовёт саппортов "
    "из пула.\n\n"
    "Ученики подключаются по ссылке от преподавателя. Админ-статус выдаёт "
    "разработчик."
)

_BOT_SHORT_DESCRIPTION = (
    "Опросы посещаемости для танцевальных школ. Перед занятием спрашивает "
    "учеников, кто придёт, и присылает админу сводку."
)


async def apply_bot_profile(bot: Bot) -> None:
    """Выставляем bot description / short_description — то, что юзер видит ДО /start
    в карточке бота и в результатах поиска. Telegram молча игнорирует
    повторное проставление того же текста, так что вызов идемпотентен."""
    try:
        await bot.set_my_description(_BOT_DESCRIPTION)
    except Exception:  # noqa: BLE001
        pass
    try:
        await bot.set_my_short_description(_BOT_SHORT_DESCRIPTION)
    except Exception:  # noqa: BLE001
        pass
