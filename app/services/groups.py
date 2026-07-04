from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Admin, Group, GroupAdmin, SchoolMember


def _gen_join_code() -> str:
    return secrets.token_urlsafe(6)


async def create_group(
    session: AsyncSession,
    school_id: int,
    name: str,
    weekdays: str,
    lesson_time: str,
    lead_hours: int,
    *,
    style: str | None = None,
    level: int | None = None,
    venue_id: int | None = None,
    room: str | None = None,
    teachers_note: str | None = None,
    created_by_admin_id: int | None = None,
) -> Group:
    """Создаёт группу. Если задан `created_by_admin_id` — автор становится
    ответственным (`GroupAdmin`) и куратором добора (`recruit_lead_admin_id`)."""
    group = Group(
        school_id=school_id,
        name=name,
        style=style,
        level=level,
        weekdays=weekdays,
        lesson_time=lesson_time,
        lead_hours=lead_hours,
        venue_id=venue_id,
        room=room,
        teachers_note=teachers_note,
        join_code=_gen_join_code(),
        recruit_lead_admin_id=created_by_admin_id,
    )
    session.add(group)
    await session.flush()
    if created_by_admin_id is not None:
        session.add(GroupAdmin(group_id=group.id, admin_id=created_by_admin_id))
        await session.flush()
    return group


async def set_recruit_lead(
    session: AsyncSession, group_id: int, admin_id: int | None
) -> None:
    """Назначить/сменить куратора добора группы."""
    group = await session.get(Group, group_id)
    if group is not None:
        group.recruit_lead_admin_id = admin_id
        await session.flush()


async def resolve_recruit_lead(session: AsyncSession, group: Group) -> Admin | None:
    """Куратор добора группы: назначенный lead, иначе fallback на owner школы.
    None если ни того, ни другого нет."""
    if group.recruit_lead_admin_id is not None:
        lead = await session.get(Admin, group.recruit_lead_admin_id)
        if lead is not None:
            return lead
    if group.school_id is None:
        return None
    from app.services import admins as admins_svc
    from app.services import schools as schools_svc
    owner_tg = await schools_svc.get_owner_tg_id(session, group.school_id)
    return await admins_svc.get_by_tg_id(session, owner_tg) if owner_tg else None


_UNSET = object()


async def close_orphaned_survey(session: AsyncSession, group: Group) -> list[int]:
    """Правка lesson_time/weekdays группы (или TZ её школы) меняет identity
    занятия — пересчитываемый UTC-инстант. Открытый Survey этой группы осиротел
    бы (сводка/напоминания/добор продолжат жить по старому расписанию, ответы
    потеряются, ученикам уйдёт второй «придёшь?»), а будущая LessonCancellation
    «воскресла» бы под новым расписанием. Закрывает открытые Survey и чистит
    будущие cancellation'ы. Возвращает admin tg_ids для уведомления — вызывающий
    шлёт его через свой bot (см. group_admin_tg_ids)."""
    from app.services import cancellations as cancellations_svc
    from app.services import surveys as surveys_svc
    from app.services.schedule import utc_now

    closed = await surveys_svc.close_open_surveys_for_group(session, group.id)
    if not closed:
        return []
    await cancellations_svc.delete_future(session, group.id, utc_now())
    if group.school_id is None:
        return []
    return await group_admin_tg_ids(session, group.id, group.school_id)


async def update_group(
    session: AsyncSession,
    group: Group,
    *,
    name: object = _UNSET,
    style: object = _UNSET,
    level: object = _UNSET,
    weekdays: object = _UNSET,
    lesson_time: object = _UNSET,
    venue_id: object = _UNSET,
    room: object = _UNSET,
    teachers_note: object = _UNSET,
) -> list[int]:
    """Patch arbitrary fields on a Group. Sentinel `_UNSET` distinguishes
    «don't touch» from «set to None». Возвращает admin tg_ids для уведомления,
    если правка weekdays/lesson_time осиротила открытый Survey (см.
    close_orphaned_survey) — иначе пусто."""
    reschedule = (
        weekdays is not _UNSET and weekdays != group.weekdays
    ) or (
        lesson_time is not _UNSET and lesson_time != group.lesson_time
    )
    if name is not _UNSET:
        group.name = name  # type: ignore[assignment]
    if style is not _UNSET:
        group.style = style  # type: ignore[assignment]
    if level is not _UNSET:
        group.level = level  # type: ignore[assignment]
    if weekdays is not _UNSET:
        group.weekdays = weekdays  # type: ignore[assignment]
    if lesson_time is not _UNSET:
        group.lesson_time = lesson_time  # type: ignore[assignment]
    if venue_id is not _UNSET:
        group.venue_id = venue_id  # type: ignore[assignment]
    if room is not _UNSET:
        group.room = room  # type: ignore[assignment]
    if teachers_note is not _UNSET:
        group.teachers_note = teachers_note  # type: ignore[assignment]
    await session.flush()
    if reschedule:
        return await close_orphaned_survey(session, group)
    return []


async def school_styles(session: AsyncSession, school_id: int) -> list[str]:
    """Distinct non-empty style names в школе, by recency."""
    stmt = (
        select(Group.style)
        .where(Group.school_id == school_id, Group.style.is_not(None))
        .order_by(Group.id.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    seen: list[str] = []
    for s in rows:
        if s and s not in seen:
            seen.append(s)
    return seen


async def get_group(session: AsyncSession, group_id: int) -> Group | None:
    return await session.get(Group, group_id)


async def get_by_join_code(session: AsyncSession, code: str) -> Group | None:
    stmt = select(Group).where(Group.join_code == code)
    return (await session.execute(stmt)).scalar_one_or_none()


async def school_groups(session: AsyncSession, school_id: int) -> list[Group]:
    """Все группы конкретной школы."""
    stmt = select(Group).where(Group.school_id == school_id).order_by(Group.id)
    return list((await session.execute(stmt)).scalars().all())


async def admin_visible_groups(
    session: AsyncSession, admin_id: int
) -> list[Group]:
    """Все группы из всех школ, где admin — member (owner или coadmin).
    Используется в /groups, /today, /stats. Сортировка: по школе, затем id."""
    stmt = (
        select(Group)
        .join(SchoolMember, SchoolMember.school_id == Group.school_id)
        .where(SchoolMember.admin_id == admin_id)
        .order_by(Group.school_id, Group.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def my_groups(session: AsyncSession, admin_id: int) -> list[Group]:
    """Группы, за которые админ ОТВЕЧАЕТ (есть запись GroupAdmin). «Мои группы».
    Видимость всех групп школы — через карточку школы, не здесь."""
    stmt = (
        select(Group)
        .join(GroupAdmin, GroupAdmin.group_id == Group.id)
        .where(GroupAdmin.admin_id == admin_id)
        .order_by(Group.school_id, Group.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def active_groups(session: AsyncSession) -> list[Group]:
    stmt = select(Group).where(Group.active.is_(True))
    return list((await session.execute(stmt)).scalars().all())


async def delete_group(session: AsyncSession, group: Group) -> None:
    await session.delete(group)
    await session.flush()


async def set_active(session: AsyncSession, group: Group, active: bool) -> None:
    """Пауза/возобновление группы. На паузе scheduler не создаёт опросов и
    не делает recruit; уже разосланные опросы продолжают жить до lesson_at."""
    group.active = active
    await session.flush()


# ── ответственные за группу (маршрутизация админ-уведомлений) ─────────


async def group_admin_ids(session: AsyncSession, group_id: int) -> set[int]:
    """admin_id'ы, назначенные ответственными за группу. Пусто = не назначены."""
    stmt = select(GroupAdmin.admin_id).where(GroupAdmin.group_id == group_id)
    return set((await session.execute(stmt)).scalars().all())


async def set_group_admin(
    session: AsyncSession, group_id: int, admin_id: int, assigned: bool
) -> None:
    """Идемпотентно добавить/убрать ответственного за группу."""
    existing = (await session.execute(
        select(GroupAdmin).where(
            GroupAdmin.group_id == group_id, GroupAdmin.admin_id == admin_id
        )
    )).scalar_one_or_none()
    if assigned and existing is None:
        session.add(GroupAdmin(group_id=group_id, admin_id=admin_id))
    elif not assigned and existing is not None:
        await session.delete(existing)
    await session.flush()


async def group_admin_tg_ids(
    session: AsyncSession, group_id: int, school_id: int | None
) -> list[int]:
    """tg_user_id ответственных за группу. Если никто не назначен — fallback на
    всех членов школы (поведение по умолчанию, пока ответственные не расставлены)."""
    stmt = (
        select(Admin.tg_user_id)
        .join(GroupAdmin, GroupAdmin.admin_id == Admin.id)
        .where(GroupAdmin.group_id == group_id)
    )
    tgs = list((await session.execute(stmt)).scalars().all())
    if tgs:
        return tgs
    if school_id is None:
        return []
    from app.services import schools as schools_svc
    return await schools_svc.member_tg_ids(session, school_id)


async def can_manage_recruit(
    session: AsyncSession, group_id: int, school_id: int | None, admin_id: int
) -> bool:
    """Разбирать добор может «куратор» группы (recruit lead). Если lead не задан —
    fallback на любого члена школы."""
    group = await session.get(Group, group_id)
    if group is None:
        return False
    if group.recruit_lead_admin_id is not None:
        return admin_id == group.recruit_lead_admin_id
    if school_id is None:
        return False
    from app.services import schools as schools_svc
    return await schools_svc.is_member(session, school_id, admin_id)
