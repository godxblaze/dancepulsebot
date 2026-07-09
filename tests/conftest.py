import contextlib
import os

# Point the app at a throwaway Postgres DB + a schema unique to this worker
# before any app module imports settings. TEST_DATABASE_URL lets CI/docker
# (docker-compose.test.yml, port 5433) override the local-dev default.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres@127.0.0.1:5432/dancepulse_test"
)
# db_path no longer drives the DB connection (see DATABASE_URL above) but still
# names the FSM-json file (until Phase 6) and the instance-lock file — per-PID
# so concurrent pytest processes (this session's Stop-hook, another parallel
# Claude session, xdist workers) don't collide on those files.
os.environ["DB_PATH"] = f"_pytest_dance_{os.getpid()}.sqlite3"

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import event, text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

import app.db.session as _db_session  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.services import admins as _admins  # noqa: E402
from app.services import schools as _schools_svc  # noqa: E402

# Schema-per-worker isolation: PYTEST_XDIST_WORKER is "gw0"/"gw1"/... under
# `-n 8`, absent when running serially.
_worker_id = os.environ.get("PYTEST_XDIST_WORKER", "serial")
_SCHEMA = f"pytest_{_worker_id}"

# The prod engine (app.db.session.engine) is built at module-import time, before
# any asyncio event loop exists — fine for prod (one process, one loop), but
# pytest-asyncio gives each test its own loop, and asyncpg connections are
# bound to the loop they were created in ("attached to a different loop"
# RuntimeError on reuse). NullPool sidesteps this: every checkout opens a fresh
# physical connection in whichever loop is currently running, none pooled
# across loops. Reassigning the module attributes (not just a local name) so
# every caller of session_scope()/engine, including code that imports `engine`
# after this module has run, picks up the test engine.
engine = create_async_engine(_db_session.settings.database_url, poolclass=NullPool)
_db_session.engine = engine
_db_session.SessionLocal = async_sessionmaker(
    engine, expire_on_commit=False, class_=_db_session.AsyncSession
)


@event.listens_for(engine.sync_engine, "connect")
def _configure_test_connection(dbapi_connection, _record) -> None:
    """Per-connection setup for every physical connection this worker opens:
    pin search_path to this worker's own schema (isolation from other workers
    sharing the same Postgres instance/database), and skip WAL-fsync-equivalent
    durability since test data is throwaway (safe here, NOT set server-wide)."""
    cursor = dbapi_connection.cursor()
    cursor.execute(f'SET search_path TO "{_SCHEMA}"')
    cursor.execute("SET synchronous_commit TO off")
    cursor.close()


# Building the schema from scratch (CREATE SCHEMA + ~17 tables) on every test
# would dominate the suite's wall-clock. Built once per worker process instead;
# between tests we only TRUNCATE. Tests mutating DDL are marked
# @pytest.mark.schema_mutating — schema is torn down and rebuilt around them.
_schema_ready = False
_wipe_sql = ""


@pytest_asyncio.fixture
async def db(request):
    """Fresh empty schema for each test that needs the database."""
    global _schema_ready, _wipe_sql
    mutating = request.node.get_closest_marker("schema_mutating") is not None
    if _schema_ready and not mutating:
        async with engine.begin() as conn:
            await conn.execute(text(_wipe_sql))
    else:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{_SCHEMA}"'))
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        _wipe_sql = "TRUNCATE " + ", ".join(
            f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables)
        ) + " RESTART IDENTITY CASCADE"
        _schema_ready = True
    yield
    if mutating:
        _schema_ready = False


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _cleanup_test_db():
    """Drop this worker's schema and close the pool at the end of the run —
    otherwise pytest_gw0../pytest_serial schemas pile up in dancepulse_test."""
    yield
    async with engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE'))
    await engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            os.remove(os.environ["DB_PATH"] + suffix)


# Прод admins.get_or_create НЕ создаёт школу — этот шаг идёт через явный
# CreateSchool FSM в UI. Старые тесты пишут `admins.get_or_create(...)` и
# сразу же используют admin.id как school_id; чтобы их не переписывать
# массово, мокаем admins.get_or_create в тестах: после создания admin'а
# гарантируем ему owned-школу. Совпадение admin.id == school.id (оба
# auto-increment в чистой БД) держит обратную совместимость.
_original_get_or_create = _admins.get_or_create


async def _get_or_create_with_school(session, tg_user_id, tg_username, full_name):
    admin = await _original_get_or_create(session, tg_user_id, tg_username, full_name)
    await _schools_svc.get_or_create_owned_school(session, admin.id)
    return admin


@pytest.fixture(autouse=True)
def _auto_school_for_tests(monkeypatch):
    monkeypatch.setattr(_admins, "get_or_create", _get_or_create_with_school)
