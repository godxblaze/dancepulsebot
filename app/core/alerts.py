from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from app.core.config import settings
from app.services.schedule import utc_now

log = logging.getLogger("dance.alerts")

ALERT_STATE_DIR = Path("logs")
# Тот же анти-спам, что у scripts/watchdog_bot.ps1 (Send-DeveloperAlert) — не
# чаще 1 алерта в час на один key, иначе застрявшая проблема (outbox, медленный
# тик) заваливает разработчика повторным сообщением на каждой проверке.
ALERT_ANTI_SPAM_WINDOW = timedelta(hours=1)


async def alert_developer(bot, text: str) -> None:
    """Best-effort пинг всем разработчикам (settings.developer_ids) в Telegram
    о фатальной проблеме рантайма. Один упавший send (напр. кто-то из
    разработчиков не нажал /start у бота) не должен помешать остальным."""
    for dev_id in settings.developer_ids:
        try:
            await bot.send_message(dev_id, text)
        except Exception as e:  # noqa: BLE001
            log.warning("Не смог оповестить разработчика %s: %s", dev_id, e)


async def alert_developer_throttled(
    bot, key: str, text: str, *, window: timedelta = ALERT_ANTI_SPAM_WINDOW
) -> bool:
    """`alert_developer` с анти-спамом по `key` — файл-стейт (тот же паттерн,
    что watchdog_bot.ps1's Send-DeveloperAlert), один на каждый вид алерта
    (`stuck_outbox`, `slow_tick`, ...). Возвращает True, если алерт реально
    ушёл, False — если подавлен окном (полезно в тестах)."""
    state_path = ALERT_STATE_DIR / f"alert-last-{key}.txt"
    now = utc_now()
    if state_path.exists():
        try:
            last = datetime.fromisoformat(state_path.read_text(encoding="utf-8").strip())
        except ValueError:
            last = None
        if last is not None and now - last < window:
            return False
    await alert_developer(bot, text)
    # logs/ уже существует к этому моменту (setup_logging() создаёт его при
    # старте приложения, до первого тика) — отдельный mkdir тут не нужен.
    state_path.write_text(now.isoformat(), encoding="utf-8")
    return True
