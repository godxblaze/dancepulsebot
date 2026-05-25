"""Reusable fakes for handler-level tests — a fake Telegram with no network.

Imitates the parts of aiogram our handlers actually touch:
- `FakeBot.send_message` returns a `SentMessage` with `message_id` and a mutable
  `reply_markup`, so the next callback can be wired to the same keyboard.
- `FakeMessage.answer` stores replies as `SentMessage`s for the same wiring.
- `FakeCallback` carries an arbitrary `message`; for our handlers the same
  `SentMessage` works — they call `edit_text` / `edit_reply_markup` on it.
- `FakeFSM` is a minimal aiogram-compatible `FSMContext`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


class SentMessage:
    """Stand-in for an aiogram Message — mutable like the real one after edits."""

    def __init__(
        self, bot: FakeBot | None, chat_id: int, text: str, reply_markup: Any = None
    ) -> None:
        self.chat = SimpleNamespace(id=chat_id)
        self.chat_id = chat_id
        self.text = text
        self.reply_markup = reply_markup
        self._bot = bot
        self.replies: list[SentMessage] = []
        if bot is not None:
            self.message_id = bot._next_id
            bot._next_id += 1
        else:
            self.message_id = 0

    async def edit_text(self, text: str, **kwargs: Any) -> None:
        self.text = text
        if "reply_markup" in kwargs:
            self.reply_markup = kwargs["reply_markup"]

    async def edit_reply_markup(self, reply_markup: Any = None, **_: Any) -> None:
        self.reply_markup = reply_markup

    async def answer(self, text: str, **kwargs: Any) -> SentMessage:
        msg = SentMessage(self._bot, self.chat_id, text, kwargs.get("reply_markup"))
        self.replies.append(msg)
        if self._bot is not None:
            self._bot.sent.append(msg)
        return msg

    async def answer_photo(
        self, photo: Any, caption: str | None = None, **_kwargs: Any  # noqa: ARG002
    ) -> SentMessage:
        msg = SentMessage(self._bot, self.chat_id, caption or "")
        self.replies.append(msg)
        if self._bot is not None:
            self._bot.sent.append(msg)
        return msg


class FakeBot:
    def __init__(self) -> None:
        self._next_id = 1000
        self.sent: list[SentMessage] = []

    async def send_message(
        self, chat_id: int, text: str, reply_markup: Any = None, **_: Any
    ) -> SentMessage:
        msg = SentMessage(self, chat_id, text, reply_markup)
        self.sent.append(msg)
        return msg

    def last_to(self, chat_id: int) -> SentMessage:
        for m in reversed(self.sent):
            if m.chat_id == chat_id:
                return m
        raise AssertionError(f"FakeBot: no message sent to chat_id={chat_id}")

    def sent_to(self, chat_id: int) -> list[SentMessage]:
        return [m for m in self.sent if m.chat_id == chat_id]


def fake_user(tg_id: int, full_name: str = "Имя", username: str = "u") -> SimpleNamespace:
    return SimpleNamespace(id=tg_id, full_name=full_name, username=username)


class FakeMessage:
    """Incoming Message-like object for handler calls."""

    def __init__(
        self,
        user: SimpleNamespace,
        text: str | None = None,
        bot: FakeBot | None = None,
    ) -> None:
        self.from_user = user
        self.chat = SimpleNamespace(id=user.id)
        self.text = text
        self.bot = bot
        self.replies: list[SentMessage] = []

    async def answer(self, text: str, **kwargs: Any) -> SentMessage:
        msg = SentMessage(self.bot, self.chat.id, text, kwargs.get("reply_markup"))
        self.replies.append(msg)
        if self.bot is not None:
            self.bot.sent.append(msg)
        return msg

    async def answer_photo(
        self, photo: Any, caption: str | None = None, **_kwargs: Any  # noqa: ARG002
    ) -> SentMessage:
        msg = SentMessage(self.bot, self.chat.id, caption or "")
        self.replies.append(msg)
        if self.bot is not None:
            self.bot.sent.append(msg)
        return msg


class FakeCallback:
    def __init__(
        self,
        data: str,
        user: SimpleNamespace,
        message: SentMessage | None = None,
    ) -> None:
        self.data = data
        self.from_user = user
        self.message = message
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


@dataclass
class FakeFSM:
    """Minimal aiogram-compatible FSMContext."""

    state: Any = None
    data: dict[str, Any] = field(default_factory=dict)

    async def set_state(self, state: Any) -> None:
        self.state = state

    async def get_state(self) -> Any:
        return self.state

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)

    async def update_data(self, **kwargs: Any) -> dict[str, Any]:
        self.data.update(kwargs)
        return dict(self.data)

    async def clear(self) -> None:
        self.state = None
        self.data = {}
