from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Gender, Person, SupportCall, SupportCallStatus, SupportClaim


async def get_call(session: AsyncSession, call_id: int) -> SupportCall | None:
    return await session.get(SupportCall, call_id)


async def get_call_for_survey(
    session: AsyncSession, survey_id: int
) -> SupportCall | None:
    stmt = select(SupportCall).where(SupportCall.survey_id == survey_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_call(
    session: AsyncSession,
    survey_id: int,
    gender: Gender,
    needed: int,
    status: SupportCallStatus,
) -> SupportCall:
    call = SupportCall(survey_id=survey_id, gender=gender, needed=needed, status=status)
    session.add(call)
    await session.flush()
    return call


async def set_status(
    session: AsyncSession, call_id: int, status: SupportCallStatus
) -> None:
    call = await session.get(SupportCall, call_id)
    if call is not None:
        call.status = status
        await session.flush()


async def count_claims(session: AsyncSession, call_id: int) -> int:
    stmt = (
        select(func.count())
        .select_from(SupportClaim)
        .where(SupportClaim.support_call_id == call_id)
    )
    return (await session.execute(stmt)).scalar_one()


async def unclaim(
    session: AsyncSession, call_id: int, person_id: int
) -> bool:
    """Revert a person's claim. Decrements helped_count and re-opens a closed
    call so the freed slot is available again. Returns True if anything changed.
    """
    row = (
        await session.execute(
            select(SupportClaim).where(
                SupportClaim.support_call_id == call_id,
                SupportClaim.person_id == person_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    person = await session.get(Person, person_id)
    if person is not None and person.helped_count > 0:
        person.helped_count -= 1
    await session.delete(row)
    call = await session.get(SupportCall, call_id)
    if call is not None and call.status == SupportCallStatus.closed:
        call.status = SupportCallStatus.open
    await session.flush()
    return True


async def claim(
    session: AsyncSession, call_id: int, person_id: int
) -> tuple[bool, int, int]:
    """Record a support claim if there is room.

    Returns (accepted, filled, needed). `accepted` is False if the slot is already
    full or the person claimed before. Closes the call and bumps the person's
    helped_count when the claim succeeds.
    """
    call = await session.get(SupportCall, call_id)
    if call is None:
        return False, 0, 0
    dup = await session.execute(
        select(SupportClaim).where(
            SupportClaim.support_call_id == call_id,
            SupportClaim.person_id == person_id,
        )
    )
    filled = await count_claims(session, call_id)
    if dup.scalar_one_or_none() is not None:
        return False, filled, call.needed
    if filled >= call.needed:
        return False, filled, call.needed
    session.add(SupportClaim(support_call_id=call_id, person_id=person_id))
    person = await session.get(Person, person_id)
    if person is not None:
        person.helped_count += 1
    filled += 1
    if filled >= call.needed:
        call.status = SupportCallStatus.closed
    await session.flush()
    return True, filled, call.needed
