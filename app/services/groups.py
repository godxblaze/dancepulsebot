from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, GroupAccess, GroupAccessRole


def _gen_join_code() -> str:
    return secrets.token_urlsafe(6)


async def create_group(
    session: AsyncSession,
    admin_id: int,
    name: str,
    weekdays: str,
    lesson_time: str,
    lead_hours: int,
    *,
    style: str | None = None,
    level: int | None = None,
    venue_id: int | None = None,
    room: str | None = None,
    teachers_note: str | None = None,
) -> Group:
    group = Group(
        admin_id=admin_id,
        name=name,
        style=style,
        level=level,
        weekdays=weekdays,
        lesson_time=lesson_time,
        lead_hours=lead_hours,
        venue_id=venue_id,
        room=room,
        teachers_note=teachers_note,
        join_code=_gen_join_code(),
    )
    session.add(group)
    await session.flush()
    # Создатель автоматически получает owner-доступ. Сделано здесь, а не в
    # хэндлере, чтобы scripts/demo_*.py и тесты тоже всегда давали owner'а.
    session.add(
        GroupAccess(group_id=group.id, admin_id=admin_id, role=GroupAccessRole.owner)
    )
    await session.flush()
    return group


_UNSET = object()


async def update_group(
    session: AsyncSession,
    group: Group,
    *,
    name: object = _UNSET,
    style: object = _UNSET,
    level: object = _UNSET,
    weekdays: object = _UNSET,
    lesson_time: object = _UNSET,
    venue_id: object = _UNSET,
    room: object = _UNSET,
    teachers_note: object = _UNSET,
) -> None:
    """Patch arbitrary fields on a Group. Sentinel `_UNSET` distinguishes
    «don't touch» from «set to None»."""
    if name is not _UNSET:
        group.name = name  # type: ignore[assignment]
    if style is not _UNSET:
        group.style = style  # type: ignore[assignment]
    if level is not _UNSET:
        group.level = level  # type: ignore[assignment]
    if weekdays is not _UNSET:
        group.weekdays = weekdays  # type: ignore[assignment]
    if lesson_time is not _UNSET:
        group.lesson_time = lesson_time  # type: ignore[assignment]
    if venue_id is not _UNSET:
        group.venue_id = venue_id  # type: ignore[assignment]
    if room is not _UNSET:
        group.room = room  # type: ignore[assignment]
    if teachers_note is not _UNSET:
        group.teachers_note = teachers_note  # type: ignore[assignment]
    await session.flush()


async def admin_styles(session: AsyncSession, admin_id: int) -> list[str]:
    """Distinct non-empty style names the admin has previously used, by recency."""
    stmt = (
        select(Group.style)
        .where(Group.admin_id == admin_id, Group.style.is_not(None))
        .order_by(Group.id.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    seen: list[str] = []
    for s in rows:
        if s and s not in seen:
            seen.append(s)
    return seen


async def get_group(session: AsyncSession, group_id: int) -> Group | None:
    return await session.get(Group, group_id)


async def get_by_join_code(session: AsyncSession, code: str) -> Group | None:
    stmt = select(Group).where(Group.join_code == code)
    return (await session.execute(stmt)).scalar_one_or_none()


async def admin_groups(session: AsyncSession, admin_id: int) -> list[Group]:
    """Все группы, к которым у админа есть доступ (owner ИЛИ co-admin) — owner'ы
    перечислены через GroupAccess, потому одной выборкой через junction."""
    stmt = (
        select(Group)
        .join(GroupAccess, GroupAccess.group_id == Group.id)
        .where(GroupAccess.admin_id == admin_id)
        .order_by(Group.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def active_groups(session: AsyncSession) -> list[Group]:
    stmt = select(Group).where(Group.active.is_(True))
    return list((await session.execute(stmt)).scalars().all())


async def delete_group(session: AsyncSession, group: Group) -> None:
    await session.delete(group)
    await session.flush()
