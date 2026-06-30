"""Thin helpers over Telethon's Conversation for readable e2e steps.

Telethon already does the heavy lifting (Conversation, Message.click); these
just name the two common shapes so tests read cleanly.

Важно про кнопки:
- Reply-кнопки нижнего меню (🏫 Школа, 📊 Статистика, …) — это НЕ inline.
  Чтобы "нажать" такую — просто отправь её текст: ``await ask(conv, "📊 Статистика")``.
- Inline-кнопки (ответ на опрос att:*, picker саппортов, режимы /pool) —
  через ``tap_inline``: это шлёт боту настоящий callback_query.
"""
from __future__ import annotations

from telethon.tl.custom import Conversation, Message


async def ask(conv: Conversation, text: str, *, timeout: float = 15.0) -> Message:  # noqa: ASYNC109
    """Отправить `text` боту и вернуть его следующий ответ."""
    await conv.send_message(text)
    return await conv.get_response(timeout=timeout)


async def tap_inline(
    conv: Conversation, msg: Message, *, text: str, timeout: float = 15.0  # noqa: ASYNC109
) -> Message:
    """Нажать inline-кнопку с подписью `text` на `msg` (настоящий callback_query)
    и вернуть правку этого же сообщения, которую сделал бот в ответ."""
    await msg.click(text=text)
    return await conv.get_edit(timeout=timeout)
