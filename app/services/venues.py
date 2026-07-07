from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SchoolMember, Venue


async def create_venue(
    session: AsyncSession,
    school_id: int,
    name: str,
    address: str | None = None,
) -> Venue:
    venue = Venue(school_id=school_id, name=name, address=address)
    session.add(venue)
    await session.flush()
    return venue


async def get_venue(session: AsyncSession, venue_id: int) -> Venue | None:
    return await session.get(Venue, venue_id)


async def delete_venue(session: AsyncSession, venue: Venue) -> None:
    """У групп, привязанных к площадке, venue_id обнулится (FK SET NULL)."""
    await session.delete(venue)
    await session.flush()


async def school_venues(session: AsyncSession, school_id: int) -> list[Venue]:
    """Venues школы, oldest first — для venue-picker'а в мастере."""
    stmt = (
        select(Venue).where(Venue.school_id == school_id).order_by(Venue.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def admin_visible_venues(
    session: AsyncSession, admin_id: int
) -> list[Venue]:
    """Venues из всех школ admin'а."""
    stmt = (
        select(Venue)
        .join(SchoolMember, SchoolMember.school_id == Venue.school_id)
        .where(SchoolMember.admin_id == admin_id)
        .order_by(Venue.school_id, Venue.id)
    )
    return list((await session.execute(stmt)).scalars().all())
