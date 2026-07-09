"""Postgres-backed FSM storage for aiogram 3 (Phase 6).

Replaces the old whole-file-rewrite `JsonFileStorage` — under concurrent
onboarding (10-20 QR joins at once) that serialized the entire event loop
through one `asyncio.Lock` + full-file `tmp+rename`, not just dialogs.
Per-key row locking here (`SELECT ... FOR UPDATE`) means unrelated users'
concurrent dialog transitions no longer contend with each other.

`dance.fsm.json` decommission decision: start fresh, no one-time import. Per
Phase 1's own data-volume finding this pilot has trivially few active users;
any dialog genuinely mid-flight at cutover just needs the user to restart it
(a `/start`, tap "◀ Назад", or re-open the wizard) — a one-time, one-tap cost,
not worth writing a JSON→Postgres import script for. The old `.fsm.json` file
is simply no longer read or written anywhere in this module.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import FSMState
from app.db.session import session_scope
from app.services.schedule import utc_now

# Дальше этого срока заброшенный dialog-state удаляется TTL-уборкой
# (build_scheduler). 30 дней: с запасом дольше любого реального онбординга —
# при этом не даёт таблице расти вечно за счёт брошенных на середине flow'ов.
FSM_TTL_DAYS = 30


def _key_to_str(key: StorageKey) -> str:
    bc = key.business_connection_id or ""
    # destiny ОБЯЗАТЕЛЬНО включать: aiogram-dialog различает stack и per-intent
    # context именно через это поле. Без него dialog-контексты затирают друг
    # друга и при load_context падает KeyError: 'state'.
    destiny = key.destiny or "default"
    return f"{key.bot_id}:{key.chat_id}:{key.user_id}:{key.thread_id or 0}:{bc}:{destiny}"


async def _read_modify_write(k: str, mutate) -> None:
    """Read-modify-write one key's slot atomically. `SELECT ... FOR UPDATE`
    locks the row for existing keys, so concurrent writers to the SAME key
    serialize; writers to DIFFERENT keys never contend (the whole point of
    moving off one file-wide lock)."""
    async with session_scope() as s:
        row = (
            await s.execute(select(FSMState).where(FSMState.key == k).with_for_update())
        ).scalar_one_or_none()
        slot = dict(row.data) if row is not None else {}
        mutate(slot)
        if not slot:
            if row is not None:
                await s.delete(row)
            return
        if row is not None:
            row.data = slot
            return
        # Brand-new key: no row existed to lock above, so a genuine concurrent
        # first-write race is (in principle) possible — upsert reconciles it
        # last-writer-wins instead of raising a duplicate-key error. At this
        # project's scale (one dialog session per user, Telegram itself
        # serializes a single chat's updates) this is a theoretical edge, not
        # an observed one.
        await s.execute(
            pg_insert(FSMState)
            .values(key=k, data=slot)
            .on_conflict_do_update(index_elements=["key"], set_={"data": slot})
        )


class PostgresStorage(BaseStorage):
    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state

        def _mutate(slot: dict[str, Any]) -> None:
            if value is None:
                slot.pop("state", None)
            else:
                slot["state"] = value

        await _read_modify_write(_key_to_str(key), _mutate)

    async def get_state(self, key: StorageKey) -> str | None:
        async with session_scope() as s:
            row = await s.get(FSMState, _key_to_str(key))
        return (row.data or {}).get("state") if row is not None else None

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        data = dict(data)

        def _mutate(slot: dict[str, Any]) -> None:
            if data:
                slot["data"] = data
            else:
                slot.pop("data", None)

        await _read_modify_write(_key_to_str(key), _mutate)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with session_scope() as s:
            row = await s.get(FSMState, _key_to_str(key))
        return dict((row.data or {}).get("data") or {}) if row is not None else {}

    async def close(self) -> None:
        """No persistent connection/lock of our own to release — session_scope
        uses the app's shared connection pool, which app/main.py disposes."""


async def cleanup_stale(ttl_days: int = FSM_TTL_DAYS) -> int:
    """Deletes `fsm_state` rows untouched for longer than `ttl_days` — an
    abandoned dialog (user closed Telegram mid-wizard) otherwise lives
    forever. Returns the number of rows removed."""
    cutoff = utc_now() - timedelta(days=ttl_days)
    async with session_scope() as s:
        result = await s.execute(delete(FSMState).where(FSMState.updated_at < cutoff))
    return result.rowcount or 0
