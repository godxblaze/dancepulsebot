"""Типизированный парсер callback_data.

Использовать в новых callback'ах вместо ручных `data.split(":")` + `int(parts[N])`.
Уже существующий код не переписан — оставлен как есть до того момента когда
формат callback-data конкретного экрана начнёт меняться.

Пример:
    parsed = parse_cb(call.data, "pcardlvl", int, int, str)
    if parsed is None:
        await call.answer()
        return
    group_id, person_id, code = parsed
"""
from __future__ import annotations

from typing import Any


def parse_cb(
    data: str | None, prefix: str, *types: type
) -> tuple[Any, ...] | None:
    """Парсит '<prefix>:<a1>:<a2>:...' и приводит сегменты к указанным типам.

    Возвращает кортеж значений или None если:
      - data пустая
      - префикс не совпал
      - количество сегментов не совпало с числом типов
      - привести значение не удалось (ValueError/TypeError)
    """
    if not data:
        return None
    parts = data.split(":")
    if parts[0] != prefix or len(parts) != len(types) + 1:
        return None
    try:
        return tuple(t(p) for t, p in zip(types, parts[1:], strict=True))
    except (ValueError, TypeError):
        return None
