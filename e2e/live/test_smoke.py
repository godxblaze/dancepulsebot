"""Smoke e2e против ЖИВОГО @dancepulsebot по MTProto (настоящий Telegram).

Одноразовая настройка:
    1. Впиши E2E_API_ID / E2E_API_HASH в .env (https://my.telegram.org).
    2. python -m e2e.live.login   → вставь напечатанный E2E_SESSION в .env.

Запуск:
    pytest e2e/live -v
"""
from __future__ import annotations

import pytest

from e2e.config import e2e_settings
from e2e.live.helpers import ask, tap_inline


async def test_start_responds(client):
    """/start получает настоящий ответ от живого бота."""
    async with client.conversation(e2e_settings.bot_username, timeout=20) as conv:
        resp = await ask(conv, "/start")
        assert resp.text, "бот не ответил на /start"


async def test_help_responds(client):
    async with client.conversation(e2e_settings.bot_username, timeout=20) as conv:
        resp = await ask(conv, "/help")
        assert resp.text


@pytest.mark.skip(reason="шаблон под состояние твоего бота — впиши реальный текст кнопки")
async def test_tap_inline_button(client):
    """Пример настоящего клика по inline-кнопке.

    Открой нужный экран бота (например, дойди до опроса посещаемости или
    picker'а саппортов), посмотри подпись inline-кнопки и подставь её ниже.
    """
    async with client.conversation(e2e_settings.bot_username, timeout=20) as conv:
        screen = await ask(conv, "/start")
        edited = await tap_inline(conv, screen, text="<подпись inline-кнопки>")
        assert edited.text
