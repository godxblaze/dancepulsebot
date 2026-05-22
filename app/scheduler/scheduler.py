from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.bot.keyboards import attendance_keyboard
from app.core.config import settings
from app.db.models import Group, SurveyKind, SurveyStatus, Teacher
from app.db.session import session_scope
from app.services import groups as groups_svc
from app.services import students as students_svc
from app.services import surveys as surveys_svc
from app.services.schedule import local_now, next_lesson_at

log = logging.getLogger(__name__)


async def _poll_group(bot: Bot, group: Group, lesson_at: datetime) -> None:
    """Create the attendance survey for `lesson_at` and message every student once."""
    async with session_scope() as s:
        survey, created = await surveys_svc.get_or_create_survey(
            s, group.id, SurveyKind.attendance, lesson_at
        )
        if not created:
            return  # already polled this lesson
        students = await students_svc.group_students(s, group.id)
        await surveys_svc.ensure_responses(s, survey.id, [st.id for st in students])
        survey_id = survey.id

    if not students:
        log.info("Group «%s» has no students — survey %s is empty", group.name, survey_id)
        return

    text = (
        f"💓 Dance Pulse\n"
        f"Группа «{group.name}», занятие {lesson_at:%d.%m} в {group.lesson_time}.\n"
        f"Придёшь?"
    )
    sent = 0
    for st in students:
        try:
            msg = await bot.send_message(
                st.tg_user_id, text, reply_markup=attendance_keyboard(survey_id)
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to send survey to tg_user_id=%s: %s", st.tg_user_id, e)
            continue
        async with session_scope() as s:
            await surveys_svc.set_message_id(s, survey_id, st.id, msg.message_id)
        sent += 1
    log.info("Survey %s sent to %d/%d students of «%s»", survey_id, sent, len(students), group.name)


def _format_summary(group: Group, lesson_at: datetime, summary: surveys_svc.SurveySummary) -> str:
    lines = [
        "💓 Dance Pulse — итоги опроса",
        f"Группа «{group.name}», занятие {lesson_at:%d.%m} в {group.lesson_time}",
        f"придут: {summary.yes} из {summary.total}",
    ]
    if summary.no_names:
        lines.append(f"не придут: {', '.join(summary.no_names)}")
    if summary.pending_names:
        lines.append(f"не ответили: {', '.join(summary.pending_names)}")
    return "\n".join(lines)


async def _maybe_send_summary(
    bot: Bot, group: Group, lesson_at: datetime, now: datetime
) -> None:
    """Once per lesson, halfway through the poll window, send the teacher the summary."""
    if now < lesson_at - timedelta(hours=group.lead_hours / 2):
        return
    async with session_scope() as s:
        survey = await surveys_svc.find_attendance_survey(s, group.id, lesson_at)
        if survey is None or survey.status != SurveyStatus.open or survey.summary_sent:
            return
        summary = await surveys_svc.summarize(s, survey.id)
        teacher = await s.get(Teacher, group.teacher_id)
        survey_id = survey.id
        teacher_tg = teacher.tg_user_id if teacher else None

    if teacher_tg is None:
        return
    try:
        await bot.send_message(teacher_tg, _format_summary(group, lesson_at, summary))
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to send auto-summary to teacher %s: %s", teacher_tg, e)
        return  # not marked — next tick retries
    async with session_scope() as s:
        await surveys_svc.mark_summary_sent(s, survey_id)
    log.info("Auto-summary for survey %s sent to teacher %s", survey_id, teacher_tg)


async def tick(bot: Bot) -> None:
    """Runs every 15 min: close past surveys, send polls and auto-summaries within window."""
    now = local_now()
    async with session_scope() as s:
        closed = await surveys_svc.close_past_surveys(s, now)
    if closed:
        log.info("Closed %d past survey(s)", closed)

    async with session_scope() as s:
        groups = await groups_svc.active_groups(s)

    for group in groups:
        lesson_at = next_lesson_at(group.weekdays, group.lesson_time, now)
        if lesson_at is None:
            continue
        hours_until = (lesson_at - now).total_seconds() / 3600
        if 0 < hours_until <= group.lead_hours:
            await _poll_group(bot, group, lesson_at)
            await _maybe_send_summary(bot, group, lesson_at, now)


def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=settings.timezone)
    sched.add_job(
        tick,
        "interval",
        minutes=15,
        args=[bot],
        id="attendance_tick",
        next_run_time=datetime.now(ZoneInfo(settings.timezone)),
    )
    log.info("Scheduler built — attendance tick every 15 min")
    return sched
