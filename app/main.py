from __future__ import annotations

import asyncio
import logging
import signal
import sys

from app.bot.bot import build_bot, build_dispatcher
from app.bot.commands import apply_bot_profile, grant_admin_commands, set_default_commands
from app.bot.outbox_worker import run_sender_worker
from app.core.alerts import alert_developer
from app.core.config import settings
from app.core.instance_lock import InstanceLock
from app.core.logging import setup_logging
from app.db.session import init_db, session_scope
from app.scheduler.scheduler import build_scheduler
from app.services import admins as admins_svc

log = logging.getLogger("dance.main")

# Backpressure (Phase 8): без ограничения aiogram планирует по asyncio.Task на
# КАЖДЫЙ входящий update независимо — после простоя (рестарт/деплой) сотни
# накопленных апдейтов от Telegram открыли бы сотни конкурентных DB-сессий
# разом, раньше исчерпав пул, чем бот успеет разгрести очередь. Пул
# (app/db/session.py) — pool_size=10 + max_overflow=20 = 30 сессий одновременно.
# Планировщик-тик и outbox sender worker обрабатывают свою работу строго
# последовательно (не конкурентно — см. их циклы) и держат максимум по одной
# сессии каждый; 25 оставляет им и разовым фоновым job'ам (conflict sentinel,
# fsm cleanup) комфортный запас, не подходя вплотную к пределу пула.
# Используем встроенный механизм aiogram 3.27 (Dispatcher.start_polling
# tasks_concurrency_limit=...) — своя семафор-обвязка не нужна.
UPDATE_CONCURRENCY_LIMIT = 25


async def run() -> bool:
    """Возвращает True при штатном завершении (сигнал/Ctrl+C), False — при
    фатальном сбое (занятый lock, упавший polling). main() транслирует False
    в ненулевой exit-код, чтобы Task Scheduler перезапустил процесс."""
    setup_logging()
    settings.validate_runtime()

    lock = InstanceLock(f"{settings.db_path}.lock")
    if not lock.acquire():
        log.critical(
            "Уже запущен другой экземпляр бота (lock занят: %s.lock). Выхожу.",
            settings.db_path,
        )
        try:
            bot = build_bot()
            await alert_developer(
                bot, "⚠️ Второй экземпляр бота не смог стартовать — lock занят."
            )
            await bot.session.close()
        except Exception as e:  # noqa: BLE001
            log.warning("Не смог оповестить разработчика о занятом lock: %s", e)
        return False

    try:
        return await _run_locked()
    finally:
        lock.release()


async def _run_locked() -> bool:
    await init_db()
    log.info("Database ready (db=%s)", settings.db_path)

    bot = build_bot()
    await apply_bot_profile(bot)
    try:
        await set_default_commands(bot)
    except Exception as e:  # noqa: BLE001
        log.warning("set_default_commands failed (продолжаю запуск): %s", e)
    async with session_scope() as s:
        for a in await admins_svc.list_all(s):
            try:
                await grant_admin_commands(bot, a.tg_user_id)
            except Exception as e:  # noqa: BLE001
                log.warning("Could not set admin commands for %s: %s", a.tg_user_id, e)
    dp = build_dispatcher()
    # Снимаем возможный залётный webhook — иначе getUpdates молча не получает
    # ничего, и бот выглядит «мёртвым» без ошибок. drop_pending_updates=False:
    # не теряем сообщения, накопившиеся пока бот лежал.
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception as e:  # noqa: BLE001
        log.warning("delete_webhook failed (продолжаю polling): %s", e)
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

    polling = asyncio.create_task(
        dp.start_polling(
            bot, handle_signals=False, tasks_concurrency_limit=UPDATE_CONCURRENCY_LIMIT
        )
    )
    waiter = asyncio.create_task(stop_event.wait())
    # Outbox sender worker — independent of the scheduler tick/heartbeat above,
    # stopped via the same stop_event so a single signal shuts down both.
    outbox_task = asyncio.create_task(run_sender_worker(bot, stop_event))

    success = True
    try:
        await asyncio.wait(
            {polling, waiter}, return_when=asyncio.FIRST_COMPLETED
        )
        # Polling завершился сам (не по сигналу) — значит упал. Достаём исключение,
        # иначе оно молча проглатывается, и бот «лежит» без видимой причины.
        # Детект конфликта двух экземпляров — не здесь: aiogram 3.27 в
        # _listen_updates ретраит TelegramConflictError бесконечно, polling-таск
        # никогда не падает с ней. См. ConflictSentinel (app/core/sentinel.py).
        if polling.done() and not polling.cancelled():
            exc = polling.exception()
            if exc is not None:
                success = False
                log.error("Polling упал: %s", exc, exc_info=exc)
                await alert_developer(
                    bot, f"⚠️ Polling упал: {type(exc).__name__}: {exc}"
                )
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Keyboard interrupt, shutting down…")
    finally:
        log.info("Stopping polling…")
        try:
            await dp.stop_polling()
        except RuntimeError as e:
            log.debug("stop_polling: polling уже завершился (%s)", e)
        if not polling.done():
            polling.cancel()
            try:
                await polling
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        log.info("Stopping scheduler (waiting for current tick)…")
        scheduler.shutdown(wait=True)
        log.info("Stopping outbox sender worker…")
        if not outbox_task.done():
            outbox_task.cancel()
            try:
                await outbox_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await dp.storage.close()
        await bot.session.close()
        log.info("Shutdown complete")
    return success


def main() -> None:
    try:
        ok = asyncio.run(run())
    except KeyboardInterrupt:
        # На Windows без add_signal_handler — Ctrl+C прилетает сюда.
        log.info("Interrupted")
        return
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
