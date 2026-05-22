from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Gender, Group, Membership, Person


async def get_person(
    session: AsyncSession, teacher_id: int, tg_user_id: int
) -> Person | None:
    stmt = select(Person).where(
        Person.teacher_id == teacher_id, Person.tg_user_id == tg_user_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _get_or_create_person(
    session: AsyncSession,
    teacher_id: int,
    tg_user_id: int,
    tg_username: str | None,
    full_name: str,
) -> Person:
    """One Person per (teacher, telegram account); refreshes name/username."""
    person = await get_person(session, teacher_id, tg_user_id)
    if person is None:
        person = Person(
            teacher_id=teacher_id,
            tg_user_id=tg_user_id,
            tg_username=tg_username,
            full_name=full_name,
        )
        session.add(person)
        await session.flush()
        return person
    person.tg_username = tg_username
    person.full_name = full_name
    await session.flush()
    return person


async def set_gender(session: AsyncSession, person_id: int, gender: Gender) -> None:
    person = await session.get(Person, person_id)
    if person is not None:
        person.gender = gender
        await session.flush()


async def add_to_group(
    session: AsyncSession,
    group: Group,
    tg_user_id: int,
    tg_username: str | None,
    full_name: str,
) -> tuple[Person, bool]:
    """Register a person in `group`.

    Returns (person, joined) — `joined` is False if they were already a member.
    """
    person = await _get_or_create_person(
        session, group.teacher_id, tg_user_id, tg_username, full_name
    )
    stmt = select(Membership).where(
        Membership.person_id == person.id, Membership.group_id == group.id
    )
    if (await session.execute(stmt)).scalar_one_or_none() is not None:
        return person, False
    session.add(Membership(person_id=person.id, group_id=group.id))
    await session.flush()
    return person, True


async def get_in_group(
    session: AsyncSession, group_id: int, tg_user_id: int
) -> Person | None:
    stmt = (
        select(Person)
        .join(Membership, Membership.person_id == Person.id)
        .where(Membership.group_id == group_id, Person.tg_user_id == tg_user_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def group_members(session: AsyncSession, group_id: int) -> list[Person]:
    stmt = (
        select(Person)
        .join(Membership, Membership.person_id == Person.id)
        .where(Membership.group_id == group_id)
        .order_by(Person.full_name)
    )
    return list((await session.execute(stmt)).scalars().all())


async def count_members(session: AsyncSession, group_id: int) -> int:
    stmt = (
        select(func.count())
        .select_from(Membership)
        .where(Membership.group_id == group_id)
    )
    return (await session.execute(stmt)).scalar_one()
