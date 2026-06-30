"""Fixtures for the live e2e suite — a real Telegram user-account driving
@dancepulsebot over MTProto. Function-scoped client: reconnects per test
(~200 ms) to dodge pytest-asyncio session-loop footguns."""
from __future__ import annotations

import pytest_asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

from e2e.config import e2e_settings


@pytest_asyncio.fixture
async def client():
    e2e_settings.require()
    tg = TelegramClient(
        StringSession(e2e_settings.session),
        e2e_settings.api_id,
        e2e_settings.api_hash,
    )
    await tg.connect()
    if not await tg.is_user_authorized():
        await tg.disconnect()
        raise RuntimeError(
            "E2E_SESSION недействителен или протух — перелогинься: "
            "python -m e2e.live.login"
        )
    try:
        yield tg
    finally:
        await tg.disconnect()
