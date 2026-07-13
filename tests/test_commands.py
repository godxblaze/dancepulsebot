"""app/bot/commands.py — пункт #20 аудита (модуль без тестов). Проверяет, что
меню команд ставится/снимается с правильным scope и что apply_bot_profile не
падает, если Telegram отклоняет один из двух вызовов."""
from aiogram.types import BotCommandScopeChat, BotCommandScopeDefault

from app.bot.commands import (
    ADMIN_COMMANDS,
    STUDENT_COMMANDS,
    apply_bot_profile,
    grant_admin_commands,
    revoke_admin_commands,
    set_default_commands,
)


class _FakeBot:
    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self._fail = fail or set()

    async def set_my_commands(self, *args, **kwargs) -> None:
        self.calls.append(("set_my_commands", args, kwargs))

    async def delete_my_commands(self, *args, **kwargs) -> None:
        self.calls.append(("delete_my_commands", args, kwargs))

    async def set_my_description(self, *args, **kwargs) -> None:
        if "set_my_description" in self._fail:
            raise RuntimeError("telegram недоступен")
        self.calls.append(("set_my_description", args, kwargs))

    async def set_my_short_description(self, *args, **kwargs) -> None:
        if "set_my_short_description" in self._fail:
            raise RuntimeError("telegram недоступен")
        self.calls.append(("set_my_short_description", args, kwargs))


async def test_set_default_commands_uses_student_list_and_default_scope() -> None:
    bot = _FakeBot()
    await set_default_commands(bot)
    name, args, kwargs = bot.calls[0]
    assert name == "set_my_commands"
    assert args[0] == STUDENT_COMMANDS
    assert isinstance(kwargs["scope"], BotCommandScopeDefault)


async def test_grant_admin_commands_uses_admin_list_and_chat_scope() -> None:
    bot = _FakeBot()
    await grant_admin_commands(bot, 555)
    name, args, kwargs = bot.calls[0]
    assert name == "set_my_commands"
    assert args[0] == ADMIN_COMMANDS
    assert isinstance(kwargs["scope"], BotCommandScopeChat)
    assert kwargs["scope"].chat_id == 555


async def test_revoke_admin_commands_deletes_chat_scope_menu() -> None:
    bot = _FakeBot()
    await revoke_admin_commands(bot, 555)
    name, _args, kwargs = bot.calls[0]
    assert name == "delete_my_commands"
    assert isinstance(kwargs["scope"], BotCommandScopeChat)
    assert kwargs["scope"].chat_id == 555


async def test_apply_bot_profile_sets_both_descriptions() -> None:
    bot = _FakeBot()
    await apply_bot_profile(bot)
    names = [name for name, _args, _kwargs in bot.calls]
    assert names == ["set_my_description", "set_my_short_description"]


async def test_apply_bot_profile_survives_partial_failure() -> None:
    """Telegram может отклонить один из двух вызовов — второй всё равно должен
    выполниться, а функция не должна пробрасывать исключение."""
    bot = _FakeBot(fail={"set_my_description"})
    await apply_bot_profile(bot)
    names = [name for name, _args, _kwargs in bot.calls]
    assert names == ["set_my_short_description"]
