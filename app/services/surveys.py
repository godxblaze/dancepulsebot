from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Answer, Response, Student, Survey, SurveyKind, SurveyStatus


@dataclass
class SurveySummary:
    total: int
    yes: int
    no: int
    pending: int
    no_names: list[str]
    pending_names: list[str]


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
    session: AsyncSession, survey_id: int, student_ids: list[int]
) -> None:
    """Create empty (unanswered) Response rows for students that don't have one."""
    stmt = select(Response.student_id).where(Response.survey_id == survey_id)
    existing = set((await session.execute(stmt)).scalars().all())
    for sid in student_ids:
        if sid not in existing:
            session.add(Response(survey_id=survey_id, student_id=sid))
    await session.flush()


async def set_message_id(
    session: AsyncSession, survey_id: int, student_id: int, message_id: int
) -> None:
    await session.execute(
        update(Response)
        .where(Response.survey_id == survey_id, Response.student_id == student_id)
        .values(tg_message_id=message_id)
    )


async def record_answer(
    session: AsyncSession, survey_id: int, student_id: int, answer: Answer
) -> None:
    stmt = select(Response).where(
        Response.survey_id == survey_id, Response.student_id == student_id
    )
    resp = (await session.execute(stmt)).scalar_one_or_none()
    if resp is None:
        resp = Response(survey_id=survey_id, student_id=student_id)
        session.add(resp)
    resp.answer = answer
    resp.answered_at = datetime.now()
    await session.flush()


async def summarize(session: AsyncSession, survey_id: int) -> SurveySummary:
    stmt = (
        select(Response, Student)
        .join(Student, Response.student_id == Student.id)
        .where(Response.survey_id == survey_id)
        .order_by(Student.full_name)
    )
    rows = (await session.execute(stmt)).all()
    yes = no = pending = 0
    no_names: list[str] = []
    pending_names: list[str] = []
    for resp, student in rows:
        if resp.answer == Answer.yes:
            yes += 1
        elif resp.answer == Answer.no:
            no += 1
            no_names.append(student.full_name)
        else:
            pending += 1
            pending_names.append(student.full_name)
    return SurveySummary(len(rows), yes, no, pending, no_names, pending_names)


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
