from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Gender, Person, SupportCall, SupportCallStatus, SupportClaim


async def _recount_helped(session: AsyncSession, person_id: int) -> None:
    """helped_count = число claim'ов этого человека с attended=True. Денормализованный
    счётчик пересчитывается из факта явки, не из числа откликов."""
    cnt = (await session.execute(
        select(func.count())
        .select_from(SupportClaim)
        .where(SupportClaim.person_id == person_id, SupportClaim.attended.is_(True))
    )).scalar_one()
    person = await session.get(Person, person_id)
    if person is not None:
        person.helped_count = cnt
        await session.flush()


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


async def claimed_person_ids(session: AsyncSession, call_id: int) -> set[int]:
    """Person id'ы, уже согласившиеся выручить по этому call'у — чтобы не
    предлагать их повторно в picker'е добора остатка (фаза 2)."""
    stmt = select(SupportClaim.person_id).where(
        SupportClaim.support_call_id == call_id
    )
    return set((await session.execute(stmt)).scalars().all())


async def unclaim(
    session: AsyncSession, call_id: int, person_id: int
) -> bool:
    """Revert a person's claim. Re-opens a closed call so the freed slot is
    available again, and recomputes helped_count. Returns True if anything changed.
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
    await session.delete(row)
    await session.flush()
    await _recount_helped(session, person_id)
    call = await session.get(SupportCall, call_id)
    if call is not None and call.status == SupportCallStatus.closed:
        call.status = SupportCallStatus.open
    await session.flush()
    return True


async def mark_invited(session: AsyncSession, call_id: int) -> None:
    """Инвайты только что отправлены: статус → open, таймер «тишины» считаем с
    этого момента (created_at = now), флаг no_reply_notified сбрасываем."""
    call = await session.get(SupportCall, call_id)
    if call is not None:
        call.status = SupportCallStatus.open
        call.created_at = datetime.now(UTC).replace(tzinfo=None)
        call.no_reply_notified = False
        await session.flush()


async def pending_unnotified_calls(session: AsyncSession) -> list[SupportCall]:
    """Pending-call'ы (owner получил picker, но ещё не нажал «Позвать»), по
    которым его ещё не пинговали. Scheduler решает по близости занятия, пора ли
    напомнить «разбери добор»."""
    stmt = select(SupportCall).where(
        SupportCall.status == SupportCallStatus.pending,
        SupportCall.pending_notified.is_(False),
    )
    return list((await session.execute(stmt)).scalars().all())


async def mark_pending_notified(session: AsyncSession, call_id: int) -> None:
    call = await session.get(SupportCall, call_id)
    if call is not None:
        call.pending_notified = True
        await session.flush()


async def claimed_supports_for_survey(
    session: AsyncSession, survey_id: int
) -> list[tuple[Person, SupportClaim]]:
    """Согласившиеся саппорты занятия (через SupportCall) — для пост-leson
    отметки явки. Возвращает (person, claim), по имени."""
    stmt = (
        select(Person, SupportClaim)
        .join(SupportClaim, SupportClaim.person_id == Person.id)
        .join(SupportCall, SupportCall.id == SupportClaim.support_call_id)
        .where(SupportCall.survey_id == survey_id)
        .order_by(Person.full_name)
    )
    return [(p, c) for p, c in (await session.execute(stmt)).all()]


async def set_support_attended(
    session: AsyncSession, claim_id: int, attended: bool | None
) -> int | None:
    """Отметить, пришёл ли саппорт. Пересчитывает helped_count человека.
    Возвращает person_id (или None если claim не найден)."""
    claim = await session.get(SupportClaim, claim_id)
    if claim is None:
        return None
    claim.attended = attended
    await session.flush()
    await _recount_helped(session, claim.person_id)
    return claim.person_id


async def stale_open_calls(
    session: AsyncSession, now: datetime, idle_for: timedelta
) -> list[SupportCall]:
    """Open SupportCall'ы, созданные раньше чем now-idle_for, у которых ещё
    нет required-числа отклинков и preпод ещё не был уведомлён про тишину.

    Используется scheduler'ом чтобы сказать preподу «никто не откликнулся за час»."""
    cutoff = now - idle_for
    stmt = (
        select(SupportCall)
        .where(
            SupportCall.status == SupportCallStatus.open,
            SupportCall.no_reply_notified.is_(False),
            SupportCall.created_at < cutoff,
        )
    )
    return list((await session.execute(stmt)).scalars().all())


async def mark_no_reply_notified(session: AsyncSession, call_id: int) -> None:
    call = await session.get(SupportCall, call_id)
    if call is not None:
        call.no_reply_notified = True
        await session.flush()


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
    # helped_count НЕ инкрементим здесь — он считается по факту явки
    # (SupportClaim.attended), которую preпод отмечает после занятия.
    session.add(SupportClaim(support_call_id=call_id, person_id=person_id))
    filled += 1
    if filled >= call.needed:
        call.status = SupportCallStatus.closed
    await session.flush()
    return True, filled, call.needed
