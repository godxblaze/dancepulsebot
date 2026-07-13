import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.metrics import RollingStats

log = logging.getLogger(__name__)

# Phase 9 observability — живой gauge длительности сессии (создание+commit/
# rollback+закрытие), не персистится, см. app/core/metrics.py.
_session_stats = RollingStats()


def session_duration_stats() -> dict:
    return _session_stats.snapshot()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Pool sizing for "a few thousand users a month" scale: aiogram polling processes
# updates concurrently (bounded by Phase 8's backpressure semaphore), plus the
# scheduler tick and the outbox sender worker each hold a connection briefly.
# pool_size=10 + max_overflow=20 gives headroom for ~30 concurrent sessions
# before queuing, comfortably above expected concurrency at pilot→early-growth
# scale on a single Postgres instance. pool_timeout=30s fails fast instead of
# hanging a handler indefinitely if the pool is ever exhausted. pool_recycle=1800
# (30min) avoids stale connections across Postgres idle-connection timeouts /
# network equipment drops on a long-running VPS process.
# If the pool ever needs to scale past what a single asyncpg pool handles well
# on one Postgres instance, add PgBouncer in transaction mode in front of it —
# not needed at current scale, noted here for the next scale-up.
engine = create_async_engine(
    settings.database_url,
    pool_size=10,
    max_overflow=20,
    pool_timeout=30,
    pool_recycle=1800,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def _alembic_upgrade_head() -> None:
    from alembic.config import Config

    from alembic import command

    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_PROJECT_ROOT / "alembic"))
    command.upgrade(cfg, "head")
    # alembic/env.py вызывает fileConfig(alembic.ini) — тот пересобирает root-логгер
    # по секции [logger_root] (level=WARNING, handlers=console), стирая
    # RotatingFileHandler + INFO-уровень, выставленные setup_logging() при
    # старте. Без этого восстановления все .info() после первого init_db()
    # молча исчезают, dance.log обрывается на строчках alembic-миграции.
    from app.core.logging import setup_logging

    setup_logging()


async def init_db() -> None:
    """Schema/data state used to be built up via PRAGMA-gated raw-SQL migrations
    here (SQLite lightweight-migrations + one-off backfills). All of that effect
    is now captured directly in the Alembic baseline — this just runs Alembic to
    head, in a thread since `alembic.command.upgrade` is a blocking call."""
    await asyncio.to_thread(_alembic_upgrade_head)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    start = time.monotonic()
    try:
        async with SessionLocal() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    finally:
        _session_stats.record(time.monotonic() - start)
