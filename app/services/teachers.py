from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Teacher


async def get_by_tg_id(session: AsyncSession, tg_user_id: int) -> Teacher | None:
    stmt = select(Teacher).where(Teacher.tg_user_id == tg_user_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_or_create(
    session: AsyncSession,
    tg_user_id: int,
    tg_username: str | None,
    full_name: str,
) -> Teacher:
    teacher = await get_by_tg_id(session, tg_user_id)
    if teacher is None:
        teacher = Teacher(tg_user_id=tg_user_id, tg_username=tg_username, full_name=full_name)
        session.add(teacher)
        await session.flush()
    return teacher
