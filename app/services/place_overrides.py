"""Место/зал одного конкретного занятия (override поверх площадки группы)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LessonPlaceOverride


async def set_place(
    session: AsyncSession, group_id: int, lesson_at: datetime, place: str
) -> LessonPlaceOverride:
    """Upsert: повторный ввод по тому же занятию перезаписывает место."""
    existing = await get(session, group_id, lesson_at)
    if existing is not None:
        existing.place = place
        await session.flush()
        return existing
    override = LessonPlaceOverride(
        group_id=group_id, lesson_at=lesson_at, place=place
    )
    session.add(override)
    await session.flush()
    return override


async def get(
    session: AsyncSession, group_id: int, lesson_at: datetime
) -> LessonPlaceOverride | None:
    stmt = select(LessonPlaceOverride).where(
        LessonPlaceOverride.group_id == group_id,
        LessonPlaceOverride.lesson_at == lesson_at,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_place(
    session: AsyncSession, group_id: int, lesson_at: datetime
) -> str | None:
    override = await get(session, group_id, lesson_at)
    return override.place if override is not None else None


async def clear(
    session: AsyncSession, group_id: int, lesson_at: datetime
) -> bool:
    """Убрать override — занятие возвращается к месту группы."""
    override = await get(session, group_id, lesson_at)
    if override is None:
        return False
    await session.delete(override)
    await session.flush()
    return True


async def delete_future(session: AsyncSession, group_id: int, now: datetime) -> int:
    """Удаляет БУДУЩИЕ override'ы группы при правке расписания (lesson_time/
    weekdays/TZ): identity занятия меняется, место по старому расписанию не
    должно «воскреснуть» под новым (зеркало cancellations.delete_future)."""
    result = await session.execute(
        delete(LessonPlaceOverride).where(
            LessonPlaceOverride.group_id == group_id,
            LessonPlaceOverride.lesson_at > now,
        )
    )
    return result.rowcount or 0
