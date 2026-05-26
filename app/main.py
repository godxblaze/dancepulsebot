from __future__ import annotations

import asyncio
import logging
import signal

from app.bot.bot import build_bot, build_dispatcher
from app.bot.commands import grant_admin_commands, set_default_commands
from app.core.config import settings
from app.core.logging import setup_logging
from app.db.session import init_db, session_scope
from app.scheduler.scheduler import build_scheduler
from app.services import admins as admins_svc

log = logging.getLogger("dance.main")


async def run() -> None:
    setup_logging()
    settings.validate_runtime()
    await init_db()
    log.info("Database ready (db=%s)", settings.db_path)

    bot = build_bot()
    await set_default_commands(bot)
    async with session_scope() as s:
        for a in await admins_svc.list_all(s):
            try:
                await grant_admin_commands(bot, a.tg_user_id)
            except Exception as e:  # noqa: BLE001
                log.warning("Could not set admin commands for %s: %s", a.tg_user_id, e)
    dp = build_dispatcher()
    scheduler = build_scheduler(bot)
    scheduler.start()
    log.info("Bot polling started")

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _on_signal(sig: int) -> None:
        log.info("Received signal %s, shutting down…", sig)
        stop_event.set()

    # SIGTERM не существует на Windows для add_signal_handler; ловим что доступно.
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except NotImplementedError:
            # Windows ProactorEventLoop не поддерживает add_signal_handler —
            # там Ctrl+C прилетит как KeyboardInterrupt в asyncio.run().
            pass

    polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    waiter = asyncio.create_task(stop_event.wait())

    try:
        await asyncio.wait(
            {polling, waiter}, return_when=asyncio.FIRST_COMPLETED
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Keyboard interrupt, shutting down…")
    finally:
        log.info("Stopping polling…")
        await dp.stop_polling()
        if not polling.done():
            polling.cancel()
            try:
                await polling
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        log.info("Stopping scheduler (waiting for current tick)…")
        scheduler.shutdown(wait=True)
        await dp.storage.close()
        await bot.session.close()
        log.info("Shutdown complete")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        # На Windows без add_signal_handler — Ctrl+C прилетает сюда.
        log.info("Interrupted")


if __name__ == "__main__":
    main()
