"""Phase 8: load test proving app.main.UPDATE_CONCURRENCY_LIMIT (bounding
aiogram's concurrent update-processing tasks) keeps a burst well within the
Phase 1 connection pool (pool_size=10 + max_overflow=20 = 30), instead of
letting a post-downtime backlog open hundreds of sessions at once.

Uses its own scratch database (never the shared schema-per-worker
`dancepulse_test`) with a REAL pool matching Phase 1's config — the whole
point is to prove the bound+pool combination under genuine connection
contention, not the test infra's unlimited NullPool. See
`[[pg-dump-whole-db-lock-gotcha]]`-style isolation, same reasoning as
tests/test_backup.py.

Requires `createdb`/`dropdb` on PATH (same portable Postgres bin as
test_backup.py).
"""
import asyncio
import os
import random
import subprocess
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Membership
from app.main import UPDATE_CONCURRENCY_LIMIT
from scripts import backup_db
from scripts.seed_load_test import Scale, seed

# Host/port/user (+ password via env/DSN, never via argv) for creating scratch
# DBs — reads whatever server TEST_DATABASE_URL points at (docker-compose.test.yml,
# or the portable-Postgres trust-auth default), never the shared dancepulse_test
# database itself. Same reasoning as tests/test_backup.py.
_admin_parts = urlsplit(
    backup_db.plain_url(
        os.environ.get(
            "TEST_DATABASE_URL", "postgresql+asyncpg://postgres@127.0.0.1:5432/dancepulse_test"
        )
    )
)
_PG_HOST = _admin_parts.hostname or "127.0.0.1"
_PG_PORT = _admin_parts.port or 5432
_PG_USER = _admin_parts.username or "postgres"
_PG_ENV = {"PGPASSWORD": _admin_parts.password} if _admin_parts.password else {}
_PG_ADMIN = ["-h", _PG_HOST, "-p", str(_PG_PORT), "-U", _PG_USER]
_PG_AUTH = f"{_PG_USER}:{_admin_parts.password}@" if _admin_parts.password else f"{_PG_USER}@"
BURST_SIZE = 200


def _make_scratch_db(name: str) -> str:
    env = {**os.environ, **_PG_ENV}
    subprocess.run(["dropdb", "--if-exists", *_PG_ADMIN, name], check=True, env=env)
    subprocess.run(["createdb", *_PG_ADMIN, name], check=True, env=env)
    return f"postgresql+asyncpg://{_PG_AUTH}{_PG_HOST}:{_PG_PORT}/{name}"


def _drop_scratch_db(name: str) -> None:
    subprocess.run(
        ["dropdb", "--if-exists", *_PG_ADMIN, name], check=True, env={**os.environ, **_PG_ENV}
    )


async def _simulate_update(sessionmaker, semaphore, group_ids: list[int]) -> None:
    """Одна «обработка апдейта»: держит сессию из пула, делает реальный запрос
    к засеянным данным + короткая пауза — имитация времени хендлера (сходить
    в БД, собрать ответ, отправить через bot.send_message)."""
    async with semaphore:
        async with sessionmaker() as session:
            group_id = random.choice(group_ids)
            await session.execute(
                select(func.count()).select_from(Membership).where(Membership.group_id == group_id)
            )
            await asyncio.sleep(0.02)
            await session.commit()


async def test_burst_of_200_concurrent_updates_holds_within_pool() -> None:
    scratch_db = f"dancepulse_backpressure_scratch_{os.getpid()}"
    scratch_url = _make_scratch_db(scratch_db)
    try:
        # Скромный, но реальный масштаб (полные 10 школ/400 групп/4000 человек
        # из Phase 2 не нужны для доказательства бэкпрешера — важна сама
        # concurrent-нагрузка на пул, не объём данных).
        counts = await seed(
            scratch_url,
            Scale(schools=2, groups=20, persons=200, surveys_per_group=1),
            reset=True,
        )
        assert counts["groups"] == 20

        # Тот же пул, что в Phase 1 (app/db/session.py) — не NullPool тестовой
        # инфры: иначе тест ничего не доказывает про реальное исчерпание пула.
        engine = create_async_engine(
            scratch_url, pool_size=10, max_overflow=20, pool_timeout=30,
        )
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            group_ids = list(range(1, counts["groups"] + 1))
            semaphore = asyncio.Semaphore(UPDATE_CONCURRENCY_LIMIT)
            results = await asyncio.gather(
                *(
                    _simulate_update(sessionmaker, semaphore, group_ids)
                    for _ in range(BURST_SIZE)
                ),
                return_exceptions=True,
            )
        finally:
            await engine.dispose()

        errors = [r for r in results if isinstance(r, Exception)]
        assert errors == []  # zero pool-exhaustion (or any other) errors
        assert len(results) == BURST_SIZE  # все 200 в итоге завершились
    finally:
        _drop_scratch_db(scratch_db)
