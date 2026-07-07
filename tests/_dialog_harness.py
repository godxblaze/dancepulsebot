"""Test harness for the aiogram-dialog wizards.

The plain-handler fakes can't drive aiogram-dialog flows — those need a real
Dispatcher with `setup_dialogs`. This wires one up with a MockMessageManager
(so dialog windows render into memory) and a RecordingBot (so the explicit
`message.answer(...)` calls inside dialog handlers don't hit the network)."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import GetMe, TelegramMethod
from aiogram.types import Chat, InlineKeyboardMarkup, Message, User
from aiogram_dialog import setup_dialogs
from aiogram_dialog.test_tools import BotClient, MockMessageManager
from aiogram_dialog.test_tools.keyboard import InlineButtonTextLocator

from app.bot.dialogs import (
    add_teacher_preset_dialog,
    add_venue_dialog,
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
    lesson_place,
    pool,
    recruit,
    school,
    search,
    student,
    survey,
)


class RecordingBot(Bot):
    """A real Bot whose network calls are intercepted and recorded instead of
    sent. Returns plausible stubs so `message.answer(...)` etc. don't blow up."""

    def __init__(self) -> None:
        super().__init__(
            token="42:TEST", default=DefaultBotProperties()
        )
        self.records: list[TelegramMethod[Any]] = []
        self.outbox: list[Message] = []  # messages we "sent"/"edited" that carry a keyboard
        self._mid = 5000

    @property
    def id(self) -> int:  # type: ignore[override]
        return 42

    async def __call__(
        self, method: TelegramMethod[Any], request_timeout: int | None = None
    ) -> Any:
        self.records.append(method)
        if isinstance(method, GetMe):
            return User(id=42, is_bot=True, first_name="Dance Pulse", username="dancepulsebot")
        chat_id = getattr(method, "chat_id", 1) or 1
        if not isinstance(chat_id, int):
            chat_id = 1
        self._mid += 1
        # Message.reply_markup only accepts an InlineKeyboardMarkup; reply-keyboards
        # (e.g. the admin menu) aren't representable on an outbound Message stub.
        markup = getattr(method, "reply_markup", None)
        inline = markup if isinstance(markup, InlineKeyboardMarkup) else None
        msg = Message(
            message_id=self._mid,
            date=datetime.fromtimestamp(1234567890),
            chat=Chat(id=chat_id, type="private"),
            text=getattr(method, "text", None) or getattr(method, "caption", None) or "",
            reply_markup=inline,
        )
        if inline is not None:
            self.outbox.append(msg)
        return msg

    def texts(self) -> list[str]:
        out: list[str] = []
        for m in self.records:
            t = getattr(m, "text", None) or getattr(m, "caption", None)
            if t:
                out.append(t)
        return out

    def find_button_message(self, substring: str) -> Message | None:
        """Most recent outbound message whose inline keyboard has a button
        containing `substring`."""
        for msg in reversed(self.outbox):
            if msg.reply_markup is None:
                continue
            for row in msg.reply_markup.inline_keyboard:
                for btn in row:
                    if substring in (btn.text or ""):
                        return msg
        return None


# The handler routers and Dialog objects are module-level singletons — a Router
# can be attached to exactly one Dispatcher per process. So we build the
# dispatcher once and reuse it, resetting per-test state (FSM stack + rendered
# messages) on each make_client().
_DP: Dispatcher | None = None
_MOCK: MockMessageManager | None = None


def _get_dispatcher() -> tuple[Dispatcher, MockMessageManager]:
    global _DP, _MOCK
    if _DP is None:
        dp = Dispatcher(storage=MemoryStorage())
        dp.include_router(student.router)
        dp.include_router(survey.router)
        dp.include_router(recruit.router)
        dp.include_router(pool.router)
        dp.include_router(search.router)
        dp.include_router(lesson_cancel.router)
        dp.include_router(lesson_place.router)
        dp.include_router(school.router)
        dp.include_router(attendance.router)
        dp.include_router(admin.router)
        dp.include_router(new_group_dialog)
        dp.include_router(edit_group_dialog)
        dp.include_router(create_school_dialog)
        dp.include_router(rename_school_dialog)
        dp.include_router(edit_name_dialog)
        dp.include_router(broadcast_dialog)
        dp.include_router(add_venue_dialog)
        dp.include_router(add_teacher_preset_dialog)
        _MOCK = MockMessageManager()
        setup_dialogs(dp, message_manager=_MOCK)
        _DP = dp
    return _DP, _MOCK


def make_client(user_id: int) -> tuple[BotClient, MockMessageManager, RecordingBot]:
    dp, mock = _get_dispatcher()
    mock.reset_history()
    # wipe any leftover FSM / dialog stack from a previous test
    storage = dp.storage
    inner = getattr(storage, "storage", None)
    if isinstance(inner, dict):
        inner.clear()
    bot = RecordingBot()
    client = BotClient(dp, user_id=user_id, chat_id=user_id, bot=bot)
    return client, mock, bot


def loc(substring: str) -> InlineButtonTextLocator:
    """Locator that matches a dialog button whose visible text contains `substring`."""
    return InlineButtonTextLocator(f".*{re.escape(substring)}.*")


async def click(client: BotClient, mock: MockMessageManager, substring: str) -> None:
    """Click the button in the last rendered dialog window matching `substring`."""
    await client.click(mock.last_message(), loc(substring))


async def click_plain(client: BotClient, bot: RecordingBot, substring: str) -> None:
    """Click a button on a message rendered by a plain handler (school/group card,
    groups list) — i.e. NOT a dialog window, so it lives in the bot outbox."""
    msg = bot.find_button_message(substring)
    if msg is None:
        raise AssertionError(f"no outbound message has a button containing {substring!r}")
    await client.click(msg, loc(substring))
