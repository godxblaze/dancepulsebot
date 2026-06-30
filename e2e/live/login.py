"""One-time interactive login for the e2e user-account.

Run once from the repo root:

    python -m e2e.live.login

Спросит телефон + код из Telegram (и 2FA-пароль, если включён), затем
напечатает строку StringSession — вставь её в .env как E2E_SESSION.
После этого весь suite (`pytest e2e/live`) гоняется полностью headless.
"""
from __future__ import annotations

import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession

from e2e.config import e2e_settings


async def main() -> None:
    if not e2e_settings.api_id or not e2e_settings.api_hash:
        raise SystemExit(
            "Сначала впиши E2E_API_ID и E2E_API_HASH в .env "
            "(возьми на https://my.telegram.org → API development tools)."
        )
    async with TelegramClient(
        StringSession(), e2e_settings.api_id, e2e_settings.api_hash
    ) as client:
        me = await client.get_me()
        session = client.session.save()
        print("\n✅ Залогинился как:", me.username or me.first_name)
        print("\nВставь это в .env одной строкой:\n")
        print(f"E2E_SESSION={session}\n")


if __name__ == "__main__":
    asyncio.run(main())
