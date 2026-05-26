"""Отмена конкретного занятия группы."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
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
