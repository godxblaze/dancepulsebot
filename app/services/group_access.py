"""Multi-admin access to groups: owner + co-admins junction.

Каждая группа имеет ровно одного owner (создателя) и любое количество
co-admin'ов. И owner, и co-admin видят группу в /groups, получают авто-сводки
и могут пинговать молчащих; только owner может приглашать/убирать co-admin'ов
и удалять группу.
"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Admin, Group, GroupAccess, GroupAccessRole


async def grant_access(
    session: AsyncSession,
    group_id: int,
    admin_id: int,
    role: GroupAccessRole = GroupAccessRole.coadmin,
) -> GroupAccess:
    """Add (or upgrade) an admin's access to a group. Idempotent."""
    stmt = select(GroupAccess).where(
        GroupAccess.group_id == group_id, GroupAccess.admin_id == admin_id
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        if existing.role != role:
            existing.role = role
            await session.flush()
        return existing
    access = GroupAccess(group_id=group_id, admin_id=admin_id, role=role)
    session.add(access)
    await session.flush()
    return access


async def revoke_access(session: AsyncSession, group_id: int, admin_id: int) -> bool:
    """Remove an admin from a group. Returns True if something was removed.

    Owner снять нельзя через этот хелпер — это защита от случайного «убрать
    себя из своей группы и потерять единственного владельца». Передача owner'а
    отдельным action'ом (пока не реализован)."""
    stmt = select(GroupAccess).where(
        GroupAccess.group_id == group_id,
        GroupAccess.admin_id == admin_id,
        GroupAccess.role == GroupAccessRole.coadmin,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is None:
        return False
    await session.delete(existing)
    await session.flush()
    return True


async def get_role(
    session: AsyncSession, group_id: int, admin_id: int
) -> GroupAccessRole | None:
    """None — у админа вообще нет доступа к этой группе."""
    stmt = select(GroupAccess.role).where(
        GroupAccess.group_id == group_id, GroupAccess.admin_id == admin_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def has_access(session: AsyncSession, group_id: int, admin_id: int) -> bool:
    return await get_role(session, group_id, admin_id) is not None


async def is_owner(session: AsyncSession, group_id: int, admin_id: int) -> bool:
    return (await get_role(session, group_id, admin_id)) == GroupAccessRole.owner


async def admins_of_group(
    session: AsyncSession, group_id: int
) -> list[tuple[Admin, GroupAccessRole]]:
    """All admins who have access to a group, owner first then co-admins."""
    stmt = (
        select(Admin, GroupAccess.role)
        .join(GroupAccess, GroupAccess.admin_id == Admin.id)
        .where(GroupAccess.group_id == group_id)
        .order_by(GroupAccess.role, Admin.full_name)
    )
    return [(a, r) for a, r in (await session.execute(stmt)).all()]


async def coadmins_of_group(session: AsyncSession, group_id: int) -> list[Admin]:
    """Co-admins only — for the «who's helping» line in the group card."""
    stmt = (
        select(Admin)
        .join(GroupAccess, GroupAccess.admin_id == Admin.id)
        .where(
            GroupAccess.group_id == group_id,
            GroupAccess.role == GroupAccessRole.coadmin,
        )
        .order_by(Admin.full_name)
    )
    return list((await session.execute(stmt)).scalars().all())


async def admin_tg_ids_for_group(session: AsyncSession, group_id: int) -> list[int]:
    """tg_user_id'ы всех админов группы — для рассылки auto-summary."""
    stmt = (
        select(Admin.tg_user_id)
        .join(GroupAccess, GroupAccess.admin_id == Admin.id)
        .where(GroupAccess.group_id == group_id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def accessible_groups(
    session: AsyncSession, admin_id: int
) -> list[Group]:
    """Groups the admin owns or co-admins. Ordered by id (oldest first)."""
    stmt = (
        select(Group)
        .join(GroupAccess, GroupAccess.group_id == Group.id)
        .where(GroupAccess.admin_id == admin_id)
        .order_by(Group.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def remove_all_for_group(session: AsyncSession, group_id: int) -> None:
    """Called on group delete — drops all access rows."""
    await session.execute(delete(GroupAccess).where(GroupAccess.group_id == group_id))
    await session.flush()
