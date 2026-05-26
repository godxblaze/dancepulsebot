from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Gender, Group, Membership, Person, Response, Survey, SurveyStatus


async def get_person(
    session: AsyncSession, admin_id: int, tg_user_id: int
) -> Person | None:
    stmt = select(Person).where(
        Person.admin_id == admin_id, Person.tg_user_id == tg_user_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def find_by_tg(session: AsyncSession, tg_user_id: int) -> Person | None:
    """Any person with this telegram id, regardless of admin."""
    stmt = select(Person).where(Person.tg_user_id == tg_user_id).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def find_all_by_tg(session: AsyncSession, tg_user_id: int) -> list[Person]:
    """Every Person row for this telegram account (one human can be a student
    of multiple admins)."""
    stmt = select(Person).where(Person.tg_user_id == tg_user_id)
    return list((await session.execute(stmt)).scalars().all())


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
    admin_id: int,
    tg_user_id: int,
    tg_username: str | None,
    full_name: str,
) -> Person:
    """One Person per (admin, telegram account); refreshes name/username."""
    person = await get_person(session, admin_id, tg_user_id)
    if person is None:
        person = Person(
            admin_id=admin_id,
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


async def set_name_everywhere(
    session: AsyncSession, tg_user_id: int, full_name: str
) -> int:
    """Update full_name on every Person row for this telegram account.

    A person can sit under several admins, but it's the same human — the rename
    should apply to all rows so every admin sees the updated name.
    """
    persons = (
        (await session.execute(select(Person).where(Person.tg_user_id == tg_user_id)))
        .scalars()
        .all()
    )
    for p in persons:
        p.full_name = full_name
    await session.flush()
    return len(persons)


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
        session, group.admin_id, tg_user_id, tg_username, full_name
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


async def remove_from_group(
    session: AsyncSession, person_id: int, group_id: int
) -> bool:
    """Drop a person's membership in a group. Returns True if anything was removed.

    Also drops their Response rows for any still-open survey of that group, so
    they stop counting towards the next-lesson roll-call.
    """
    result = await session.execute(
        delete(Membership).where(
            Membership.person_id == person_id, Membership.group_id == group_id
        )
    )
    open_surveys = select(Survey.id).where(
        Survey.group_id == group_id, Survey.status == SurveyStatus.open
    )
    await session.execute(
        delete(Response).where(
            Response.person_id == person_id, Response.survey_id.in_(open_surveys)
        )
    )
    await session.flush()
    return (result.rowcount or 0) > 0


async def count_members(session: AsyncSession, group_id: int) -> int:
    stmt = (
        select(func.count())
        .select_from(Membership)
        .where(Membership.group_id == group_id)
    )
    return (await session.execute(stmt)).scalar_one()


async def list_persons(session: AsyncSession, admin_id: int) -> list[Person]:
    """Everyone in a admin's audience, ordered by name."""
    stmt = (
        select(Person)
        .where(Person.admin_id == admin_id)
        .order_by(Person.full_name)
    )
    return list((await session.execute(stmt)).scalars().all())


async def top_supports(
    session: AsyncSession, admin_id: int, limit: int = 3
) -> list[Person]:
    """Топ saппортов admin'а по helped_count (только те, у кого был хотя бы один)."""
    stmt = (
        select(Person)
        .where(Person.admin_id == admin_id, Person.helped_count > 0)
        .order_by(Person.helped_count.desc(), Person.full_name)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def long_absent(
    session: AsyncSession, admin_id: int, since: datetime
) -> list[tuple[Person, datetime | None]]:
    """Persons из активных групп admin'а, у которых последняя посещаемость
    раньше `since` (или вообще не зафиксирована). Возвращает (person, last_seen)."""

    active_member_ids = (
        select(Membership.person_id)
        .join(Group, Group.id == Membership.group_id)
        .where(Group.admin_id == admin_id, Group.active.is_(True))
        .distinct()
    )
    last_seen_stmt = (
        select(Response.person_id, func.max(Survey.lesson_at).label("last"))
        .join(Survey, Survey.id == Response.survey_id)
        .where(Response.actually_attended.is_(True))
        .group_by(Response.person_id)
    )
    last_seen = dict((await session.execute(last_seen_stmt)).all())
    persons_stmt = (
        select(Person)
        .where(Person.admin_id == admin_id, Person.id.in_(active_member_ids))
        .order_by(Person.full_name)
    )
    persons = list((await session.execute(persons_stmt)).scalars().all())
    result: list[tuple[Person, datetime | None]] = []
    for p in persons:
        last = last_seen.get(p.id)
        if last is None or last < since:
            result.append((p, last))
    return result


async def support_candidates(
    session: AsyncSession, admin_id: int, gender: Gender, exclude_group_id: int
) -> list[Person]:
    """Pool members of the given gender who can be asked to support a lesson — the
    admin's support pool (support_level set), excluding regular members of that
    group. Highest skill level first, then name.
    """
    members = select(Membership.person_id).where(
        Membership.group_id == exclude_group_id
    )
    stmt = (
        select(Person)
        .where(
            Person.admin_id == admin_id,
            Person.gender == gender,
            Person.support_level.is_not(None),
            Person.id.not_in(members),
        )
        .order_by(Person.support_level.desc(), Person.full_name)
    )
    return list((await session.execute(stmt)).scalars().all())
