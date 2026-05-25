from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Venue


async def create_venue(
    session: AsyncSession,
    teacher_id: int,
    name: str,
    address: str | None = None,
) -> Venue:
    venue = Venue(teacher_id=teacher_id, name=name, address=address)
    session.add(venue)
    await session.flush()
    return venue


async def get_venue(session: AsyncSession, venue_id: int) -> Venue | None:
    return await session.get(Venue, venue_id)


async def teacher_venues(session: AsyncSession, teacher_id: int) -> list[Venue]:
    """Venues a teacher has used, oldest first — used to offer them as picks."""
    stmt = (
        select(Venue).where(Venue.teacher_id == teacher_id).order_by(Venue.id)
    )
    return list((await session.execute(stmt)).scalars().all())
