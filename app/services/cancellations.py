"""Отмена конкретного занятия группы."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LessonCancellation


async def cancel(
    session: AsyncSession,
    group_id: int,
    lesson_at: datetime,
    reason: str | None = None,
) -> LessonCancellation:
    """Идемпотентно: если уже отменено — возвращает существующую запись."""
    existing = await get(session, group_id, lesson_at)
    if existing is not None:
        return existing
    cancellation = LessonCancellation(
        group_id=group_id, lesson_at=lesson_at, reason=reason
    )
    session.add(cancellation)
    await session.flush()
    return cancellation


async def get(
    session: AsyncSession, group_id: int, lesson_at: datetime
) -> LessonCancellation | None:
    stmt = select(LessonCancellation).where(
        LessonCancellation.group_id == group_id,
        LessonCancellation.lesson_at == lesson_at,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def is_cancelled(
    session: AsyncSession, group_id: int, lesson_at: datetime
) -> bool:
    return await get(session, group_id, lesson_at) is not None


async def delete_future(session: AsyncSession, group_id: int, now: datetime) -> int:
    """Удаляет БУДУЩИЕ LessonCancellation этой группы — используется при правке
    расписания (lesson_time/weekdays/TZ школы): identity занятия меняется,
    старая отмена по старому расписанию не должна «воскреснуть» под новым."""
    result = await session.execute(
        delete(LessonCancellation).where(
            LessonCancellation.group_id == group_id,
            LessonCancellation.lesson_at > now,
        )
    )
    return result.rowcount or 0
