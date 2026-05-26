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
