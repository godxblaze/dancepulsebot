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


async def find_by_tg(session: AsyncSession, tg_user_id: int) -> Person | None:
    """Any person with this telegram id, regardless of teacher."""
    stmt = select(Person).where(Person.tg_user_id == tg_user_id).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def person_groups(session: AsyncSession, person_id: int) -> list[Group]:
    """Groups a person is a member of."""
    stmt = (
        select(Group)
        .join(Membership, Membership.group_id == Group.id)
        .where(Membership.person_id == person_id)
        .order_by(Group.id)
    )
    return list((await session.execute(stmt)).scalars().all())


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


async def set_support_level(
    session: AsyncSession, person_id: int, level: int | None
) -> None:
    """Set a person's support skill level. None removes them from the pool."""
    person = await session.get(Person, person_id)
    if person is not None:
        person.support_level = level
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


async def list_persons(session: AsyncSession, teacher_id: int) -> list[Person]:
    """Everyone in a teacher's audience, ordered by name."""
    stmt = (
        select(Person)
        .where(Person.teacher_id == teacher_id)
        .order_by(Person.full_name)
    )
    return list((await session.execute(stmt)).scalars().all())


async def support_candidates(
    session: AsyncSession, teacher_id: int, gender: Gender, exclude_group_id: int
) -> list[Person]:
    """Pool members of the given gender who can be asked to support a lesson — the
    teacher's support pool (support_level set), excluding regular members of that
    group. Highest skill level first, then name.
    """
    members = select(Membership.person_id).where(
        Membership.group_id == exclude_group_id
    )
    stmt = (
        select(Person)
        .where(
            Person.teacher_id == teacher_id,
            Person.gender == gender,
            Person.support_level.is_not(None),
            Person.id.not_in(members),
        )
        .order_by(Person.support_level.desc(), Person.full_name)
    )
    return list((await session.execute(stmt)).scalars().all())
