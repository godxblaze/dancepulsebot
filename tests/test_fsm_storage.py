"""Tests for the Postgres-backed FSM storage (Phase 6): restart-survival,
destiny-key isolation (aiogram-dialog stack vs. per-intent context), row
cleanup on empty state/data, and the TTL cleanup job."""
from datetime import timedelta

from sqlalchemy import select

from app.bot.fsm_storage import PostgresStorage, cleanup_stale
from app.db.models import FSMState
from app.db.session import session_scope
from app.services.schedule import utc_now

KEY_ARGS = dict(bot_id=1, chat_id=100, user_id=100)


def _key(**overrides):
    from aiogram.fsm.storage.base import StorageKey

    return StorageKey(**{**KEY_ARGS, **overrides})


async def test_set_get_state_and_data_roundtrip(db) -> None:
    storage = PostgresStorage()
    key = _key()
    await storage.set_state(key, "Survey:answer")
    await storage.set_data(key, {"survey_id": 42})

    assert await storage.get_state(key) == "Survey:answer"
    assert await storage.get_data(key) == {"survey_id": 42}


async def test_restart_survival(db) -> None:
    """Write через один инстанс storage, читать через новый — storage сам
    не хранит state в памяти (каждый вызов бьёт в БД), так что новый
    инстанс = симуляция рестарта процесса без потери данных."""
    key = _key()
    writer = PostgresStorage()
    await writer.set_state(key, "NewGroup:name")
    await writer.set_data(key, {"school_id": 7})

    reader = PostgresStorage()  # «новый процесс после рестарта»
    assert await reader.get_state(key) == "NewGroup:name"
    assert await reader.get_data(key) == {"school_id": 7}


async def test_destiny_key_isolation(db) -> None:
    """Тот же bot/chat/user, разные destiny (aiogram-dialog stack vs. default
    FSM) — состояния не должны затирать друг друга."""
    storage = PostgresStorage()
    stack_key = _key(destiny="dialog_stack")
    default_key = _key(destiny="default")

    await storage.set_state(stack_key, "stack-state")
    await storage.set_data(stack_key, {"stack": [1, 2]})
    await storage.set_state(default_key, "default-state")
    await storage.set_data(default_key, {"foo": "bar"})

    assert await storage.get_state(stack_key) == "stack-state"
    assert await storage.get_data(stack_key) == {"stack": [1, 2]}
    assert await storage.get_state(default_key) == "default-state"
    assert await storage.get_data(default_key) == {"foo": "bar"}


async def test_clearing_state_and_data_deletes_row(db) -> None:
    """Когда и state, и data становятся пустыми — строка удаляется, а не
    остаётся мусором в таблице (тот же контракт, что был у JsonFileStorage)."""
    storage = PostgresStorage()
    key = _key()
    await storage.set_state(key, "Some:state")
    await storage.set_data(key, {"x": 1})

    await storage.set_state(key, None)
    await storage.set_data(key, {})

    assert await storage.get_state(key) is None
    assert await storage.get_data(key) == {}
    async with session_scope() as s:
        row = await s.get(FSMState, "1:100:100:0::default")
    assert row is None


async def test_ttl_cleanup_removes_only_stale_rows(db) -> None:
    now = utc_now()
    async with session_scope() as s:
        s.add(FSMState(key="old-key", data={"state": "stale"}))
        s.add(FSMState(key="fresh-key", data={"state": "alive"}))
    async with session_scope() as s:
        rows = (await s.execute(select(FSMState))).scalars().all()
        for row in rows:
            if row.key == "old-key":
                row.updated_at = now - timedelta(days=31)
            else:
                row.updated_at = now - timedelta(days=1)

    removed = await cleanup_stale(ttl_days=30)
    assert removed == 1

    async with session_scope() as s:
        remaining = {r.key for r in (await s.execute(select(FSMState))).scalars().all()}
    assert remaining == {"fresh-key"}
