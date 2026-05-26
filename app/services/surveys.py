from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Answer,
    Gender,
    Person,
    Response,
    Survey,
    SurveyKind,
    SurveyStatus,
)


@dataclass
class SurveySummary:
    total: int
    yes: int
    no: int
    pending: int
    no_names: list[str]
    pending_names: list[str]
    # Опционально, заполняется после занятия. None — ни одного актуального
    # ответа не отмечено (preпод ещё не нажимал «Отметить посещаемость»).
    actual_attended: int | None = None
    actual_marked: int = 0  # сколько вообще получили attendance-метку


async def get_survey(session: AsyncSession, survey_id: int) -> Survey | None:
    return await session.get(Survey, survey_id)


async def find_attendance_survey(
    session: AsyncSession, group_id: int, lesson_at: datetime
) -> Survey | None:
    stmt = select(Survey).where(
        Survey.group_id == group_id,
        Survey.kind == SurveyKind.attendance,
        Survey.lesson_at == lesson_at,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_or_create_survey(
    session: AsyncSession, group_id: int, kind: SurveyKind, lesson_at: datetime
) -> tuple[Survey, bool]:
    stmt = select(Survey).where(
        Survey.group_id == group_id, Survey.kind == kind, Survey.lesson_at == lesson_at
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing, False
    survey = Survey(group_id=group_id, kind=kind, lesson_at=lesson_at)
    session.add(survey)
    await session.flush()
    return survey, True


async def ensure_responses(
    session: AsyncSession, survey_id: int, person_ids: list[int]
) -> None:
    """Create empty (unanswered) Response rows for persons that don't have one."""
    stmt = select(Response.person_id).where(Response.survey_id == survey_id)
    existing = set((await session.execute(stmt)).scalars().all())
    for pid in person_ids:
        if pid not in existing:
            session.add(Response(survey_id=survey_id, person_id=pid))
    await session.flush()


async def set_message_id(
    session: AsyncSession, survey_id: int, person_id: int, message_id: int
) -> None:
    await session.execute(
        update(Response)
        .where(Response.survey_id == survey_id, Response.person_id == person_id)
        .values(tg_message_id=message_id)
    )


async def record_answer(
    session: AsyncSession, survey_id: int, person_id: int, answer: Answer
) -> None:
    stmt = select(Response).where(
        Response.survey_id == survey_id, Response.person_id == person_id
    )
    resp = (await session.execute(stmt)).scalar_one_or_none()
    if resp is None:
        resp = Response(survey_id=survey_id, person_id=person_id)
        session.add(resp)
    resp.answer = answer
    resp.answered_at = datetime.now()
    await session.flush()


async def summarize(session: AsyncSession, survey_id: int) -> SurveySummary:
    stmt = (
        select(Response, Person)
        .join(Person, Response.person_id == Person.id)
        .where(Response.survey_id == survey_id)
        .order_by(Person.full_name)
    )
    rows = (await session.execute(stmt)).all()
    yes = no = pending = 0
    no_names: list[str] = []
    pending_names: list[str] = []
    actual_attended = 0
    actual_marked = 0
    for resp, person in rows:
        if resp.answer == Answer.yes:
            yes += 1
        elif resp.answer == Answer.no:
            no += 1
            no_names.append(person.full_name)
        else:
            pending += 1
            pending_names.append(person.full_name)
        if resp.actually_attended is not None:
            actual_marked += 1
            if resp.actually_attended:
                actual_attended += 1
    return SurveySummary(
        len(rows), yes, no, pending, no_names, pending_names,
        actual_attended=actual_attended if actual_marked else None,
        actual_marked=actual_marked,
    )


async def close_past_surveys(session: AsyncSession, now: datetime) -> int:
    result = await session.execute(
        update(Survey)
        .where(Survey.lesson_at < now, Survey.status == SurveyStatus.open)
        .values(status=SurveyStatus.closed)
    )
    return result.rowcount or 0


async def mark_summary_sent(session: AsyncSession, survey_id: int) -> None:
    await session.execute(
        update(Survey).where(Survey.id == survey_id).values(summary_sent=True)
    )


async def recent_closed_surveys(
    session: AsyncSession, group_id: int, limit: int = 5
) -> list[Survey]:
    """Closed attendance surveys, newest first."""
    stmt = (
        select(Survey)
        .where(
            Survey.group_id == group_id,
            Survey.kind == SurveyKind.attendance,
            Survey.status == SurveyStatus.closed,
        )
        .order_by(Survey.lesson_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def pending_persons(session: AsyncSession, survey_id: int) -> list[Person]:
    """People in the survey who haven't answered yet."""
    stmt = (
        select(Person)
        .join(Response, Response.person_id == Person.id)
        .where(Response.survey_id == survey_id, Response.answer.is_(None))
        .order_by(Person.full_name)
    )
    return list((await session.execute(stmt)).scalars().all())


async def attendance_roster(
    session: AsyncSession, survey_id: int
) -> list[tuple[Person, Response]]:
    """Все Person'ы с их Response для конкретного survey — для post-lesson UI.

    Возвращает ВСЕХ, кто был в опросе (включая «нет» и молчащих). preпод
    решает кто реально был. Если хочется поглядывать только обещавших —
    отфильтровать по resp.answer == Answer.yes на стороне вызывающего."""
    stmt = (
        select(Response, Person)
        .join(Person, Response.person_id == Person.id)
        .where(Response.survey_id == survey_id)
        .order_by(Person.full_name)
    )
    rows = (await session.execute(stmt)).all()
    return [(p, r) for r, p in rows]


async def set_actual_attendance(
    session: AsyncSession, survey_id: int, person_id: int, attended: bool | None
) -> None:
    await session.execute(
        update(Response)
        .where(Response.survey_id == survey_id, Response.person_id == person_id)
        .values(actually_attended=attended)
    )


async def add_walk_in(
    session: AsyncSession, survey_id: int, person_id: int
) -> None:
    """Walk-in: незаписанный человек пришёл на занятие. Создаём/обновляем Response
    с actually_attended=True и answer=None (он ничего не обещал — просто появился)."""
    existing = (await session.execute(
        select(Response).where(
            Response.survey_id == survey_id, Response.person_id == person_id
        )
    )).scalar_one_or_none()
    if existing is None:
        session.add(Response(
            survey_id=survey_id, person_id=person_id, actually_attended=True,
        ))
    else:
        existing.actually_attended = True
    await session.flush()


async def persons_without_message(
    session: AsyncSession, survey_id: int
) -> list[Person]:
    """People whose Response row has no tg_message_id yet — the scheduler still
    owes them a poll message. Used to safely retry across ticks without double-send."""
    stmt = (
        select(Person)
        .join(Response, Response.person_id == Person.id)
        .where(Response.survey_id == survey_id, Response.tg_message_id.is_(None))
        .order_by(Person.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def gender_yes_counts(session: AsyncSession, survey_id: int) -> tuple[int, int]:
    """(male, female) — how many of each gender answered 'yes' to the survey."""
    stmt = (
        select(Person.gender, func.count())
        .join(Response, Response.person_id == Person.id)
        .where(Response.survey_id == survey_id, Response.answer == Answer.yes)
        .group_by(Person.gender)
    )
    counts = {g: c for g, c in (await session.execute(stmt)).all()}
    return counts.get(Gender.male, 0), counts.get(Gender.female, 0)
