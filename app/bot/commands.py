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
