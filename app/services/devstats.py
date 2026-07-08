"""Per-school агрегация вовлечённости за окно WINDOW — единый снапшот для
/devstats, scripts/dev_stats.py и scripts/stats_dashboard.py: формат вывода у
каждого свой, цифры считаются здесь и потому совпадают."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Answer,
    Group,
    Response,
    School,
    SchoolMember,
    SupportCall,
    SupportCallStatus,
    Survey,
)

WINDOW = timedelta(weeks=4)


async def school_row(session: AsyncSession, school: School, since: datetime) -> dict:
    """Срез одной школы: масштаб (админы/активные группы), последняя
    активность (aware-UTC | None), отклик/посещаемость/no-show за окно
    и зависший добор."""
    n_admins = (await session.execute(
        select(func.count()).select_from(SchoolMember)
        .where(SchoolMember.school_id == school.id)
    )).scalar_one()
    n_groups = (await session.execute(
        select(func.count()).select_from(Group)
        .where(Group.school_id == school.id, Group.active.is_(True))
    )).scalar_one()
    last_activity = (await session.execute(
        select(func.max(Survey.created_at))
        .select_from(Survey)
        .join(Group, Survey.group_id == Group.id)
        .where(
            Group.school_id == school.id,
            # Backfill (scheduler.backfill_missed_surveys) заводит closed Survey
            # без единой Response-строки задним числом — его created_at=«сейчас»
            # ложно омолодил бы стухшую школу. Считаем активностью только
            # survey, по которому реально кого-то опросили.
            select(Response.id).where(Response.survey_id == Survey.id).exists(),
        )
    )).scalar_one()
    # SQLite отдаёт naive-UTC — нормализуем к aware (конвенция persons.py),
    # иначе потребители падают на сравнении с aware-датами.
    if last_activity is not None and last_activity.tzinfo is None:
        last_activity = last_activity.replace(tzinfo=UTC)
    total, yes, marked, attended, noshow = (await session.execute(
        select(
            func.count(Response.id),
            func.sum(case((Response.answer == Answer.yes, 1), else_=0)),
            func.sum(case((Response.actually_attended.is_not(None), 1), else_=0)),
            func.sum(case((Response.actually_attended.is_(True), 1), else_=0)),
            func.sum(case((
                (Response.answer == Answer.yes)
                & Response.actually_attended.is_(False), 1), else_=0)),
        )
        .select_from(Response)
        .join(Survey, Response.survey_id == Survey.id)
        .join(Group, Survey.group_id == Group.id)
        .where(
            Group.school_id == school.id,
            Survey.lesson_at >= since,
            Response.answer.is_not(None),
        )
    )).one()
    stuck_calls = (await session.execute(
        select(func.count())
        .select_from(SupportCall)
        .join(Survey, SupportCall.survey_id == Survey.id)
        .join(Group, Survey.group_id == Group.id)
        .where(
            Group.school_id == school.id,
            Survey.lesson_at >= since,
            SupportCall.status.in_((SupportCallStatus.pending, SupportCallStatus.open)),
        )
    )).scalar_one()
    return {
        "school": school,
        "admins": n_admins,
        "groups": n_groups,
        "last_activity": last_activity,
        "responded": total or 0,
        "yes": yes or 0,
        "marked": marked or 0,
        "attended": attended or 0,
        "noshow": noshow or 0,
        "stuck_calls": stuck_calls,
    }


async def all_school_rows(session: AsyncSession, since: datetime) -> list[dict]:
    """Срезы всех школ в порядке school.id — сортировку под свой вывод каждый
    потребитель делает сам."""
    schools = (await session.execute(select(School).order_by(School.id))).scalars().all()
    return [await school_row(session, school, since) for school in schools]


async def active_persons(session: AsyncSession, school_id: int, since: datetime) -> int:
    """Сколько разных учеников школы хоть раз ответили на опрос за окно —
    надстройка дашборда поверх school_row."""
    return (await session.execute(
        select(func.count(func.distinct(Response.person_id)))
        .select_from(Response)
        .join(Survey, Response.survey_id == Survey.id)
        .join(Group, Survey.group_id == Group.id)
        .where(
            Group.school_id == school_id,
            Survey.lesson_at >= since,
            Response.answered_at.is_not(None),
        )
    )).scalar_one()
