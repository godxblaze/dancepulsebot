"""Школы: создание, членство, приглашения.

Школа — top-level контейнер: владеет группами, учениками, площадками, пулом
саппортов. Любой member школы видит весь её контент с правами по своей роли:
- owner: всё включая удалить школу и убрать любого co-admin'а;
- coadmin: всё операционное, но не может удалить школу или owner'а.

Pilot-правило: один Admin может быть owner ровно одной школы (своей). Co-admin'ом —
в любом числе чужих. Проверка в create_school.
"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Admin,
    School,
    SchoolInvite,
    SchoolMember,
    SchoolMemberRole,
)

INVITE_TTL = timedelta(hours=24)
DEFAULT_NAME = "Моя школа"


class AlreadyOwnsSchoolError(Exception):
    """Admin уже владеет собственной школой — повторно создать нельзя."""


# ── schools ──────────────────────────────────────────────────────────


async def create_school(
    session: AsyncSession,
    owner_admin_id: int,
    name: str = DEFAULT_NAME,
) -> School:
    """Создать школу и сразу сделать owner_admin её владельцем. Проверяет
    one-owned-school rule (пилот)."""
    existing = (await session.execute(
        select(SchoolMember).where(
            SchoolMember.admin_id == owner_admin_id,
            SchoolMember.role == SchoolMemberRole.owner,
        )
    )).scalar_one_or_none()
    if existing is not None:
        raise AlreadyOwnsSchoolError(
            f"admin {owner_admin_id} уже владеет школой {existing.school_id}"
        )
    school = School(name=name)
    session.add(school)
    await session.flush()
    session.add(SchoolMember(
        school_id=school.id, admin_id=owner_admin_id, role=SchoolMemberRole.owner,
    ))
    await session.flush()
    return school


async def get_or_create_owned_school(
    session: AsyncSession, admin_id: int, name: str = DEFAULT_NAME
) -> School:
    """Идемпотентная обёртка: вернёт собственную школу admin'а или создаст её."""
    owned = await get_owned_school(session, admin_id)
    if owned is not None:
        return owned
    return await create_school(session, admin_id, name)


async def get_school(session: AsyncSession, school_id: int) -> School | None:
    return await session.get(School, school_id)


async def get_owned_school(
    session: AsyncSession, admin_id: int
) -> School | None:
    """Школа, в которой admin — owner. None если такой нет (нестандартно для
    пилотa, но возможно сразу после /grant до auto-create)."""
    stmt = (
        select(School)
        .join(SchoolMember, SchoolMember.school_id == School.id)
        .where(
            SchoolMember.admin_id == admin_id,
            SchoolMember.role == SchoolMemberRole.owner,
        )
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def primary_school_id(session: AsyncSession, admin_id: int) -> int | None:
    """Школа, чей контент считаем «рабочим» для admin'а: собственная, а если
    её нет (co-admin без owned-школы) — первая, где он member. None если
    admin ни в одной школе. Используется там, где раньше слепо брали
    get_owned_school и co-admin получал b""/пустой список (/export, топ
    саппортов, давно отсутствующие)."""
    owned = await get_owned_school(session, admin_id)
    if owned is not None:
        return owned.id
    schools = await admin_schools(session, admin_id)
    return schools[0][0].id if schools else None


async def admin_schools(
    session: AsyncSession, admin_id: int
) -> list[tuple[School, SchoolMemberRole]]:
    """Все школы admin'а с его ролью. Owner-школа всегда первая."""
    stmt = (
        select(School, SchoolMember.role)
        .join(SchoolMember, SchoolMember.school_id == School.id)
        .where(SchoolMember.admin_id == admin_id)
        # .desc(): роль хранится строкой ("owner"/"coadmin") — обычный
        # (возрастающий) order_by дал бы "coadmin" раньше "owner" лексикографически.
        .order_by(SchoolMember.role.desc(), School.id)
    )
    return [(s, r) for s, r in (await session.execute(stmt)).all()]


async def rename_school(
    session: AsyncSession, school_id: int, new_name: str
) -> None:
    school = await session.get(School, school_id)
    if school is not None:
        school.name = new_name
        await session.flush()


async def set_timezone(
    session: AsyncSession, school_id: int, tz_name: str
) -> list[int]:
    """Меняет IANA timezone школы. Валидность tz_name проверяется на стороне
    вызывающего (UI-picker предлагает фиксированный список). TZ входит в
    identity занятия (пересчитываемый UTC-инстант) — смена осиротила бы открытые
    Survey всех групп школы; закрывает их через groups_svc.close_orphaned_survey
    (та же логика, что при правке lesson_time/weekdays одной группы). Возвращает
    deduped admin tg_ids для уведомления — вызывающий шлёт через свой bot."""
    school = await session.get(School, school_id)
    if school is None:
        return []
    school.timezone = tz_name
    await session.flush()
    from app.services import groups as groups_svc

    notify: set[int] = set()
    for group in await groups_svc.school_groups(session, school_id):
        notify.update(await groups_svc.close_orphaned_survey(session, group))
    return list(notify)


async def get_timezone(session: AsyncSession, school_id: int) -> str:
    """Имя TZ школы, или 'Europe/Moscow' если школа не найдена."""
    school = await session.get(School, school_id)
    if school is None:
        return "Europe/Moscow"
    return school.timezone or "Europe/Moscow"


async def delete_school(session: AsyncSession, school: School) -> None:
    """ON DELETE CASCADE снесёт members/invites/groups/persons/venues."""
    await session.delete(school)
    await session.flush()


# ── members ──────────────────────────────────────────────────────────


async def get_role(
    session: AsyncSession, school_id: int, admin_id: int
) -> SchoolMemberRole | None:
    stmt = select(SchoolMember.role).where(
        SchoolMember.school_id == school_id, SchoolMember.admin_id == admin_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def is_member(session: AsyncSession, school_id: int, admin_id: int) -> bool:
    return await get_role(session, school_id, admin_id) is not None


async def is_owner(session: AsyncSession, school_id: int, admin_id: int) -> bool:
    return await get_role(session, school_id, admin_id) == SchoolMemberRole.owner


async def add_coadmin(
    session: AsyncSession, school_id: int, admin_id: int
) -> SchoolMember:
    """Идемпотентно: уже coadmin/owner → возвращает существующую запись."""
    stmt = select(SchoolMember).where(
        SchoolMember.school_id == school_id, SchoolMember.admin_id == admin_id,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing
    member = SchoolMember(
        school_id=school_id, admin_id=admin_id, role=SchoolMemberRole.coadmin,
    )
    session.add(member)
    await session.flush()
    return member


async def remove_coadmin(
    session: AsyncSession, school_id: int, admin_id: int
) -> bool:
    """Снимает coadmin-членство. Owner'а через этот хелпер не снимешь —
    защита от случайного потерять-школу. Заодно чистит его как ответственного
    и куратора добора групп этой школы (см. clear_admin_from_school) — иначе
    он остаётся получателем recruit-пингов и блокирует реальных членов школы."""
    stmt = select(SchoolMember).where(
        SchoolMember.school_id == school_id,
        SchoolMember.admin_id == admin_id,
        SchoolMember.role == SchoolMemberRole.coadmin,
    )
    member = (await session.execute(stmt)).scalar_one_or_none()
    if member is None:
        return False
    await session.delete(member)
    await session.flush()
    from app.services import groups as groups_svc
    await groups_svc.clear_admin_from_school(session, school_id, admin_id)
    return True


async def members_of(
    session: AsyncSession, school_id: int
) -> list[tuple[Admin, SchoolMemberRole]]:
    stmt = (
        select(Admin, SchoolMember.role)
        .join(SchoolMember, SchoolMember.admin_id == Admin.id)
        .where(SchoolMember.school_id == school_id)
        # .desc(): роль хранится строкой ("owner"/"coadmin") — обычный
        # order_by дал бы "coadmin" раньше "owner" лексикографически (см. admin_schools).
        .order_by(SchoolMember.role.desc(), Admin.full_name)
    )
    return [(a, r) for a, r in (await session.execute(stmt)).all()]


async def coadmins_of(session: AsyncSession, school_id: int) -> list[Admin]:
    """Только coadmin'ы — для UI «убрать из школы»."""
    stmt = (
        select(Admin)
        .join(SchoolMember, SchoolMember.admin_id == Admin.id)
        .where(
            SchoolMember.school_id == school_id,
            SchoolMember.role == SchoolMemberRole.coadmin,
        )
        .order_by(Admin.full_name)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_owner_tg_id(
    session: AsyncSession, school_id: int
) -> int | None:
    """tg_user_id владельца школы. None если owner-membership отсутствует."""
    stmt = (
        select(Admin.tg_user_id)
        .join(SchoolMember, SchoolMember.admin_id == Admin.id)
        .where(
            SchoolMember.school_id == school_id,
            SchoolMember.role == SchoolMemberRole.owner,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def member_tg_ids(session: AsyncSession, school_id: int) -> list[int]:
    """tg_user_id всех member'ов школы — для fan-out рассылок."""
    stmt = (
        select(Admin.tg_user_id)
        .join(SchoolMember, SchoolMember.admin_id == Admin.id)
        .where(SchoolMember.school_id == school_id)
    )
    return list((await session.execute(stmt)).scalars().all())


# ── invites ──────────────────────────────────────────────────────────


def _gen_invite_code() -> str:
    return secrets.token_urlsafe(8)


async def create_invite(session: AsyncSession, school_id: int) -> SchoolInvite:
    invite = SchoolInvite(
        school_id=school_id,
        code=_gen_invite_code(),
        expires_at=datetime.now(UTC) + INVITE_TTL,
    )
    session.add(invite)
    await session.flush()
    return invite


async def get_invite(session: AsyncSession, code: str) -> SchoolInvite | None:
    stmt = select(SchoolInvite).where(SchoolInvite.code == code)
    return (await session.execute(stmt)).scalar_one_or_none()


def invite_is_valid(invite: SchoolInvite) -> bool:
    if invite.used_at is not None:
        return False
    expires = invite.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires > datetime.now(UTC)


async def consume_invite(
    session: AsyncSession, invite: SchoolInvite, admin_id: int
) -> bool:
    """Атомарно помечает инвайт использованным. False — кто-то уже успел
    (двое приняли одну ссылку одновременно); вызывающий не должен добавлять
    co-admin'а в этом случае. Проверка-и-действие в одном UPDATE ... WHERE,
    а не check-then-act — иначе оба конкурентных accept'а видят used_at=None."""
    result = await session.execute(
        update(SchoolInvite)
        .where(SchoolInvite.id == invite.id, SchoolInvite.used_at.is_(None))
        .values(used_at=datetime.now(UTC), used_by_admin_id=admin_id)
    )
    await session.flush()
    return result.rowcount > 0
