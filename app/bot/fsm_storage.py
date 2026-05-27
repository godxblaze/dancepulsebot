"""Простое JSON-файловое хранилище FSM для aiogram 3.

Зачем: дефолтное MemoryStorage теряет состояние при рестарте — пользователь,
застрявший на середине мастера группы или онбординга, потеряет шаг. Redis/Mongo
для пилота избыточны; SQLite-storage из коробки в aiogram 3.27 нет.

Записи мало (один-два юзера одновременно проходят мастер), потому держим
всё в памяти и переписываем файл целиком на каждое изменение через tmp+rename.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey


def _key_to_str(key: StorageKey) -> str:
    bc = key.business_connection_id or ""
    # destiny ОБЯЗАТЕЛЬНО включать: aiogram-dialog различает stack и per-intent
    # context именно через это поле. Без него dialog-контексты затирают друг
    # друга и при load_context падает KeyError: 'state'.
    destiny = key.destiny or "default"
    return f"{key.bot_id}:{key.chat_id}:{key.user_id}:{key.thread_id or 0}:{bc}:{destiny}"


class JsonFileStorage(BaseStorage):
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._data: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _flush(self) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        async with self._lock:
            k = _key_to_str(key)
            value = state.state if isinstance(state, State) else state
            slot = self._data.setdefault(k, {})
            if value is None:
                slot.pop("state", None)
            else:
                slot["state"] = value
            if not slot:
                self._data.pop(k, None)
            self._flush()

    async def get_state(self, key: StorageKey) -> str | None:
        return self._data.get(_key_to_str(key), {}).get("state")

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        async with self._lock:
            k = _key_to_str(key)
            slot = self._data.setdefault(k, {})
            if data:
                slot["data"] = data
            else:
                slot.pop("data", None)
            if not slot:
                self._data.pop(k, None)
            self._flush()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        return dict(self._data.get(_key_to_str(key), {}).get("data") or {})

    async def close(self) -> None:
        async with self._lock:
            self._flush()
