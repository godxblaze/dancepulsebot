from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Gender,
    Group,
    Membership,
    Person,
    Response,
    SchoolMember,
    SupportPoolEntry,
    Survey,
    SurveyStatus,
)


async def get_person(
    session: AsyncSession, school_id: int, tg_user_id: int
) -> Person | None:
    stmt = select(Person).where(
        Person.school_id == school_id, Person.tg_user_id == tg_user_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def find_by_tg(session: AsyncSession, tg_user_id: int) -> Person | None:
    """Any person with this telegram id, regardless of school."""
    stmt = select(Person).where(Person.tg_user_id == tg_user_id).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def find_all_by_tg(session: AsyncSession, tg_user_id: int) -> list[Person]:
    """Every Person row for this telegram account — один человек может быть
    учеником в нескольких школах."""
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


async def groups_for_tg(session: AsyncSession, tg_user_id: int) -> list[Group]:
    """Все группы телеграм-аккаунта по ВСЕМ школам. Один человек = отдельная
    Person-строка в каждой школе, поэтому person_groups(одной строки) теряет
    группы остальных школ — для экранов ученика берём по tg_user_id."""
    stmt = (
        select(Group)
        .join(Membership, Membership.group_id == Group.id)
        .join(Person, Person.id == Membership.person_id)
        .where(Person.tg_user_id == tg_user_id)
        .order_by(Group.school_id, Group.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _get_or_create_person(
    session: AsyncSession,
    school_id: int,
    tg_user_id: int,
    tg_username: str | None,
    full_name: str,
) -> Person:
    """One Person per (school, telegram account); refreshes name/username."""
    person = await get_person(session, school_id, tg_user_id)
    if person is None:
        person = Person(
            school_id=school_id,
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


async def ensure_person(
    session: AsyncSession,
    school_id: int,
    tg_user_id: int,
    tg_username: str | None,
    full_name: str,
) -> Person:
    """Person в школе БЕЗ привязки к группе — для онбординга саппорта по
    персональной ссылке (он не висит фейковым учеником ни в одной группе)."""
    return await _get_or_create_person(
        session, school_id, tg_user_id, tg_username, full_name
    )


async def set_gender(session: AsyncSession, person_id: int, gender: Gender) -> None:
    person = await session.get(Person, person_id)
    if person is not None:
        person.gender = gender
        await session.flush()


async def set_name_everywhere(
    session: AsyncSession, tg_user_id: int, full_name: str
) -> int:
    """Update full_name на всех Person-строках с этим TG-account'ом."""
    persons = (
        (await session.execute(select(Person).where(Person.tg_user_id == tg_user_id)))
        .scalars()
        .all()
    )
    for p in persons:
        p.full_name = full_name
    await session.flush()
    return len(persons)


async def set_pool_level(
    session: AsyncSession, admin_id: int, person_id: int, level: int | None
) -> None:
    """Уровень человека в ЛИЧНОМ пуле препода. None — убрать из пула."""
    entry = (await session.execute(
        select(SupportPoolEntry).where(
            SupportPoolEntry.admin_id == admin_id,
            SupportPoolEntry.person_id == person_id,
        )
    )).scalar_one_or_none()
    if level is None:
        if entry is not None:
            await session.delete(entry)
    elif entry is None:
        session.add(SupportPoolEntry(admin_id=admin_id, person_id=person_id, level=level))
    else:
        entry.level = level
    await session.flush()


async def admin_pool_levels(session: AsyncSession, admin_id: int) -> dict[int, int]:
    """{person_id: level} — пул конкретного препода. Для отрисовки /pool."""
    rows = await session.execute(
        select(SupportPoolEntry.person_id, SupportPoolEntry.level).where(
            SupportPoolEntry.admin_id == admin_id
        )
    )
    return {pid: lvl for pid, lvl in rows.all()}


async def add_to_group(
    session: AsyncSession,
    group: Group,
    tg_user_id: int,
    tg_username: str | None,
    full_name: str,
) -> tuple[Person, bool]:
    """Register a person in `group`. Школа берётся из самой group."""
    person = await _get_or_create_person(
        session, group.school_id, tg_user_id, tg_username, full_name
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


async def list_persons(session: AsyncSession, school_id: int) -> list[Person]:
    """Все persons школы, отсортированы по имени."""
    stmt = (
        select(Person)
        .where(Person.school_id == school_id)
        .order_by(Person.full_name)
    )
    return list((await session.execute(stmt)).scalars().all())


async def admin_visible_persons(
    session: AsyncSession, admin_id: int
) -> list[Person]:
    """Все persons из всех школ admin'а. /pool и pickers рисуют через это."""
    stmt = (
        select(Person)
        .join(SchoolMember, SchoolMember.school_id == Person.school_id)
        .where(SchoolMember.admin_id == admin_id)
        .order_by(Person.school_id, Person.full_name)
    )
    return list((await session.execute(stmt)).scalars().all())


async def top_supports(
    session: AsyncSession, school_id: int, limit: int = 3
) -> list[Person]:
    """Топ саппортов школы по helped_count (>0)."""
    stmt = (
        select(Person)
        .where(Person.school_id == school_id, Person.helped_count > 0)
        .order_by(Person.helped_count.desc(), Person.full_name)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def long_absent(
    session: AsyncSession, school_id: int, since: datetime
) -> list[tuple[Person, datetime | None]]:
    """Persons из активных групп школы, у которых последняя actually_attended
    раньше `since` (или вообще нет). Возвращает (person, last_seen)."""
    active_member_ids = (
        select(Membership.person_id)
        .join(Group, Group.id == Membership.group_id)
        .where(Group.school_id == school_id, Group.active.is_(True))
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
        .where(Person.school_id == school_id, Person.id.in_(active_member_ids))
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
    """ЛИЧНЫЙ пул препода нужного пола, исключая регулярных членов группы.
    Высший level первым."""
    members = select(Membership.person_id).where(
        Membership.group_id == exclude_group_id
    )
    stmt = (
        select(Person)
        .join(SupportPoolEntry, SupportPoolEntry.person_id == Person.id)
        .where(
            SupportPoolEntry.admin_id == admin_id,
            Person.gender == gender,
            Person.id.not_in(members),
        )
        .order_by(SupportPoolEntry.level.desc(), Person.full_name)
    )
    return list((await session.execute(stmt)).scalars().all())


async def school_pool_candidates(
    session: AsyncSession,
    school_id: int,
    gender: Gender,
    exclude_group_id: int,
    exclude_person_ids: set[int],
    exclude_admin_id: int | None = None,
) -> list[Person]:
    """Саппорты школы из пулов ДРУГИХ преподов («➕ из пула коллег»): люди школы
    нужного пола, у кого есть запись в чужом пуле (не lead'а — `exclude_admin_id`),
    кроме уже показанных и членов группы."""
    members = select(Membership.person_id).where(
        Membership.group_id == exclude_group_id
    )
    in_some_pool = select(SupportPoolEntry.person_id)
    if exclude_admin_id is not None:
        in_some_pool = in_some_pool.where(
            SupportPoolEntry.admin_id != exclude_admin_id
        )
    stmt = (
        select(Person)
        .where(
            Person.school_id == school_id,
            Person.gender == gender,
            Person.id.in_(in_some_pool),
            Person.id.not_in(members),
        )
        .order_by(Person.full_name)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [p for p in rows if p.id not in exclude_person_ids]
