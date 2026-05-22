from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group


def _gen_join_code() -> str:
    return secrets.token_urlsafe(6)


async def create_group(
    session: AsyncSession,
    teacher_id: int,
    name: str,
    weekdays: str,
    lesson_time: str,
    lead_hours: int,
) -> Group:
    group = Group(
        teacher_id=teacher_id,
        name=name,
        weekdays=weekdays,
        lesson_time=lesson_time,
        lead_hours=lead_hours,
        join_code=_gen_join_code(),
    )
    session.add(group)
    await session.flush()
    return group


async def get_group(session: AsyncSession, group_id: int) -> Group | None:
    return await session.get(Group, group_id)


async def get_by_join_code(session: AsyncSession, code: str) -> Group | None:
    stmt = select(Group).where(Group.join_code == code)
    return (await session.execute(stmt)).scalar_one_or_none()


async def teacher_groups(session: AsyncSession, teacher_id: int) -> list[Group]:
    stmt = select(Group).where(Group.teacher_id == teacher_id).order_by(Group.id)
    return list((await session.execute(stmt)).scalars().all())


async def active_groups(session: AsyncSession) -> list[Group]:
    stmt = select(Group).where(Group.active.is_(True))
    return list((await session.execute(stmt)).scalars().all())


async def delete_group(session: AsyncSession, group: Group) -> None:
    await session.delete(group)
    await session.flush()
