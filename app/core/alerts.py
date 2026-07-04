from __future__ import annotations

import logging

from app.core.config import settings

log = logging.getLogger("dance.alerts")


async def alert_developer(bot, text: str) -> None:
    """Best-effort пинг разработчику в Telegram о фатальной проблеме рантайма."""
    if settings.developer_id <= 0:
        return
    try:
        await bot.send_message(settings.developer_id, text)
    except Exception as e:  # noqa: BLE001
        log.warning("Не смог оповестить разработчика: %s", e)
