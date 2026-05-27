from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Admin


async def get_by_tg_id(session: AsyncSession, tg_user_id: int) -> Admin | None:
    stmt = select(Admin).where(Admin.tg_user_id == tg_user_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_all(session: AsyncSession) -> list[Admin]:
    return list((await session.execute(select(Admin))).scalars().all())


async def get_or_create(
    session: AsyncSession,
    tg_user_id: int,
    tg_username: str | None,
    full_name: str,
) -> Admin:
    """Создаёт или возвращает Admin'а. Школу автоматически НЕ создаёт —
    дальше идёт через явный flow: developer /start → CreateSchool FSM
    с вводом имени; co-admin приглашение → школа берётся из invite'а."""
    admin = await get_by_tg_id(session, tg_user_id)
    if admin is None:
        admin = Admin(tg_user_id=tg_user_id, tg_username=tg_username, full_name=full_name)
        session.add(admin)
        await session.flush()
    return admin


async def set_name(session: AsyncSession, admin_id: int, full_name: str) -> None:
    admin = await session.get(Admin, admin_id)
    if admin is not None:
        admin.full_name = full_name
        await session.flush()


async def set_support_threshold(
    session: AsyncSession, admin_id: int, threshold: int
) -> None:
    admin = await session.get(Admin, admin_id)
    if admin is not None:
        admin.support_threshold = threshold
        await session.flush()


async def set_support_cutoff_hours(
    session: AsyncSession, admin_id: int, hours: int
) -> None:
    admin = await session.get(Admin, admin_id)
    if admin is not None:
        admin.support_cutoff_hours = hours
        await session.flush()


async def delete_admin(session: AsyncSession, admin_id: int) -> bool:
    """Полностью снести Admin-запись. Возвращает True если что-то удалено.

    Используется когда человек больше не member ни одной школы — Admin без
    школ всё равно бесполезен, а оставлять его «на потом» означает что при
    повторном /grant или partner-invite get_or_create вернёт старый объект
    с has_completed_setup=True и старым именем. Чище удалить совсем."""
    admin = await session.get(Admin, admin_id)
    if admin is None:
        return False
    await session.delete(admin)
    await session.flush()
    return True
