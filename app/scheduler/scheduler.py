from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNotFound
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.bot.keyboards import (
    attendance_keyboard,
    lesson_card_keyboard,
    support_pick_keyboard,
)
from app.bot.utils import bar, format_group_title, format_place
from app.core.config import settings
from app.db.models import Gender, Group, SupportCallStatus, SurveyKind, SurveyStatus
from app.db.session import session_scope
from app.services import cancellations as cancellations_svc
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import schools as schools_svc
from app.services import supports as supports_svc
from app.services import surveys as surveys_svc
from app.services import venues as venues_svc
from app.services.schedule import local_now, next_lesson_at

log = logging.getLogger(__name__)


async def _poll_group(bot: Bot, group: Group, lesson_at: datetime) -> None:
    """Ensure the survey exists, then message everyone who hasn't been polled yet.

    Идемпотентно по пере-tick'у: tg_message_id на Response отмечает «опрос отправлен»,
    поэтому следующий tick не повторит отправку тем, кто уже получил, но дошлёт
    тем, кому не успели (новые члены / упавший send в прошлый раз)."""
    async with session_scope() as s:
        survey, _ = await surveys_svc.get_or_create_survey(
            s, group.id, SurveyKind.attendance, lesson_at
        )
        members = await persons_svc.group_members(s, group.id)
        await surveys_svc.ensure_responses(s, survey.id, [p.id for p in members])
        to_poll = await surveys_svc.persons_without_message(s, survey.id)
        survey_id = survey.id
        venue = (
            await venues_svc.get_venue(s, group.venue_id)
            if group.venue_id is not None
            else None
        )

    if not to_poll:
        return  # all members already polled

    place = format_place(group, venue)
    lines = [
        f"💓 <b>{escape(format_group_title(group))}</b>",
        f"Занятие {lesson_at:%d.%m} в {group.lesson_time}.",
    ]
    if place:
        lines.append(f"📍 {escape(place)}")
    if group.teachers_note:
        lines.append(f"👤 {escape(group.teachers_note)}")
    lines.append("")
    lines.append("Придёшь?")
    text = "\n".join(lines)
    sent = 0
    for p in to_poll:
        try:
            msg = await bot.send_message(
                p.tg_user_id,
                text,
                reply_markup=attendance_keyboard(survey_id),
                parse_mode=ParseMode.HTML,
            )
        except (TelegramForbiddenError, TelegramNotFound) as e:
            # Terminal: пользователь заблокировал бота или чат недоступен.
            # Помечаем -1, чтобы persons_without_message больше его не возвращал.
            log.info(
                "survey=%s person=%s tg=%s — terminal delivery failure (%s); "
                "marking as undeliverable",
                survey_id, p.id, p.tg_user_id, type(e).__name__,
            )
            async with session_scope() as s:
                await surveys_svc.set_message_id(s, survey_id, p.id, -1)
            continue
        except TelegramBadRequest as e:
            # «chat not found» / «user is deactivated» прилетают сюда — тоже terminal.
            msg_l = str(e).lower()
            if any(k in msg_l for k in ("not found", "deactivated", "blocked")):
                log.info(
                    "survey=%s person=%s tg=%s — terminal (%s); marking as undeliverable",
                    survey_id, p.id, p.tg_user_id, e,
                )
                async with session_scope() as s:
                    await surveys_svc.set_message_id(s, survey_id, p.id, -1)
                continue
            log.warning(
                "survey=%s person=%s tg=%s — BadRequest (transient?), will retry: %s",
                survey_id, p.id, p.tg_user_id, e,
            )
            continue
        except Exception as e:  # noqa: BLE001
            log.warning(
                "survey=%s person=%s tg=%s — send failed, will retry: %s",
                survey_id, p.id, p.tg_user_id, e,
            )
            continue
        try:
            async with session_scope() as s:
                await surveys_svc.set_message_id(s, survey_id, p.id, msg.message_id)
        except Exception as e:  # noqa: BLE001
            log.error(
                "survey=%s person=%s tg=%s — message sent (mid=%s) but message_id "
                "persistence failed, risk of re-send next tick: %s",
                survey_id, p.id, p.tg_user_id, msg.message_id, e,
            )
        sent += 1
    log.info(
        "Survey %s polled %d/%d outstanding member(s) of «%s»",
        survey_id, sent, len(to_poll), group.name,
    )


def _format_summary(group: Group, lesson_at: datetime, summary: surveys_svc.SurveySummary) -> str:
    lines = [
        f"💓 <b>{escape(group.name)}</b> — итоги опроса",
        f"занятие {lesson_at:%d.%m} в {group.lesson_time}",
        "",
        f"придут <b>{summary.yes}/{summary.total}</b>  "
        f"{bar(summary.yes, summary.total)}",
    ]
    if summary.no_names:
        lines.append(f"❌ не придут: {escape(', '.join(summary.no_names))}")
    if summary.pending_names:
        lines.append(f"⏳ молчат: {escape(', '.join(summary.pending_names))}")
    return "\n".join(lines)


async def _maybe_send_summary(
    bot: Bot, group: Group, lesson_at: datetime, now: datetime
) -> None:
    """Once per lesson, halfway through the poll window, send every admin of the
    group the same auto-summary. mark_summary_sent ставится только после хотя бы
    одной успешной отправки, чтобы недосланные адресаты доехали на след. tick."""
    if now < lesson_at - timedelta(hours=group.lead_hours / 2):
        return
    async with session_scope() as s:
        survey = await surveys_svc.find_attendance_survey(s, group.id, lesson_at)
        if survey is None or survey.status != SurveyStatus.open or survey.summary_sent:
            return
        summary = await surveys_svc.summarize(s, survey.id)
        admin_tg_ids = (
            await schools_svc.member_tg_ids(s, group.school_id)
            if group.school_id else []
        )
        survey_id = survey.id

    if not admin_tg_ids:
        return
    text = _format_summary(group, lesson_at, summary)
    markup = lesson_card_keyboard(group.id, survey_id, summary.pending)
    delivered = 0
    terminal = 0
    for tg in admin_tg_ids:
        try:
            await bot.send_message(tg, text, parse_mode=ParseMode.HTML, reply_markup=markup)
            delivered += 1
        except (TelegramForbiddenError, TelegramNotFound):
            log.info("Admin tg=%s заблокировал бота — auto-summary skipped", tg)
            terminal += 1
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to send auto-summary to admin %s: %s", tg, e)
    if delivered == 0 and terminal == 0:
        return  # все попытки транзиентно упали — попробуем на следующем tick
    # Хотя бы один админ получил, или все заблокировали бота — больше нет смысла пытаться.
    async with session_scope() as s:
        await surveys_svc.mark_summary_sent(s, survey_id)
    log.info(
        "Auto-summary for survey %s sent to %d/%d admin(s)",
        survey_id, delivered, len(admin_tg_ids),
    )


def _gender_plural(gender: Gender) -> str:
    return "парни" if gender == Gender.male else "девушки"


async def _maybe_recruit(
    bot: Bot, group: Group, lesson_at: datetime, now: datetime
) -> None:
    """На отсечке считаем баланс пола; если перекос — спрашиваем owner'а школы
    «добрать саппортов?». Одно решение на занятие."""
    if group.school_id is None:
        return
    async with session_scope() as s:
        owner_tg = await schools_svc.get_owner_tg_id(s, group.school_id)
        if owner_tg is None:
            return
        # Берём threshold/cutoff с owner'а школы — они хранятся на самом Admin'е.
        from app.services import admins as admins_svc
        owner_row = await admins_svc.get_by_tg_id(s, owner_tg)
        if owner_row is None:
            return
        if now < lesson_at - timedelta(hours=owner_row.support_cutoff_hours):
            return
        survey = await surveys_svc.find_attendance_survey(s, group.id, lesson_at)
        if survey is None:
            return
        if await supports_svc.get_call_for_survey(s, survey.id) is not None:
            return  # decision already taken for this lesson
        male, female = await surveys_svc.gender_yes_counts(s, survey.id)
        deficit = abs(male - female)
        if deficit < owner_row.support_threshold:
            await supports_svc.create_call(
                s, survey.id, Gender.male, 0, SupportCallStatus.closed
            )
            return
        gender_needed = Gender.male if male < female else Gender.female
        call = await supports_svc.create_call(
            s, survey.id, gender_needed, deficit, SupportCallStatus.pending
        )
        call_id = call.id
        teacher_tg = owner_tg
        candidates = await persons_svc.support_candidates(
            s, group.school_id, gender_needed, group.id
        )
        venue = (
            await venues_svc.get_venue(s, group.venue_id)
            if group.venue_id is not None
            else None
        )

    total_pairs = male + female
    head_lines = [
        f"🆘 <b>{escape(format_group_title(group))}</b> · "
        f"{lesson_at:%d.%m} {group.lesson_time}",
    ]
    place = format_place(group, venue)
    if place:
        head_lines.append(f"📍 {escape(place)}")
    head_lines.extend([
        "баланс пар:",
        f"🕺 {bar(male, total_pairs)} {male}",
        f"💃 {bar(female, total_pairs)} {female}",
        f"не хватает {deficit} — нужны {_gender_plural(gender_needed)}.",
    ])
    head = "\n".join(head_lines)
    try:
        if candidates:
            await bot.send_message(
                teacher_tg,
                head + "\n\nОтметь, кого позвать:",
                reply_markup=support_pick_keyboard(call_id, candidates, set()),
                parse_mode=ParseMode.HTML,
            )
        else:
            await bot.send_message(
                teacher_tg,
                head + "\n\nВ пуле нет подходящих саппортов — добавь через /pool.",
                parse_mode=ParseMode.HTML,
            )
    except Exception as e:  # noqa: BLE001
        log.warning("Could not ask teacher %s about supports: %s", teacher_tg, e)


SUPPORT_NO_REPLY_AFTER = timedelta(hours=1)


async def _notify_stale_support_calls(bot: Bot, now: datetime) -> None:
    """Если SupportCall в open уже больше SUPPORT_NO_REPLY_AFTER, а откликов
    ещё нет/мало — пингуем preпода: «никто не откликнулся, попробуй сам»."""
    async with session_scope() as s:
        # SupportCall.created_at в SQLite хранится без таймзоны (server_default=now()),
        # потому сравниваем naive UTC vs naive UTC.
        now_utc = datetime.now(UTC).replace(tzinfo=None)
        stale = await supports_svc.stale_open_calls(s, now_utc, SUPPORT_NO_REPLY_AFTER)
        items: list[tuple[int, int, int, str]] = []  # needed, filled, admin_tg, title
        for call in stale:
            filled = await supports_svc.count_claims(s, call.id)
            if filled >= call.needed:
                # Уже хватает — просто пометим чтобы не пинговать.
                await supports_svc.mark_no_reply_notified(s, call.id)
                continue
            survey = await surveys_svc.get_survey(s, call.survey_id)
            if survey is None:
                continue
            group = await groups_svc.get_group(s, survey.group_id)
            if group is None:
                continue
            admin_tgs = (
                await schools_svc.member_tg_ids(s, group.school_id)
                if group.school_id else []
            )
            if not admin_tgs:
                continue
            title = format_group_title(group)
            for tg in admin_tgs:
                items.append((call.needed, filled, tg, title))
            await supports_svc.mark_no_reply_notified(s, call.id)

    for needed, filled, tg, title in items:
        text = (
            f"🆘 «{escape(title)}» — никто из приглашённых не откликнулся "
            f"за час ({filled}/{needed}). Стоит написать им лично или поискать "
            f"саппорта через /pool."
        )
        try:
            await bot.send_message(tg, text, parse_mode=ParseMode.HTML)
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to send stale-support notice to admin %s: %s", tg, e)


async def tick(bot: Bot) -> None:
    """Runs every 15 min: close past surveys, send polls, auto-summaries and recruit."""
    now = local_now()
    async with session_scope() as s:
        closed = await surveys_svc.close_past_surveys(s, now)
    if closed:
        log.info("Closed %d past survey(s)", closed)

    await _notify_stale_support_calls(bot, now)

    async with session_scope() as s:
        groups = await groups_svc.active_groups(s)

    for group in groups:
        lesson_at = next_lesson_at(group.weekdays, group.lesson_time, now)
        if lesson_at is None:
            continue
        hours_until = (lesson_at - now).total_seconds() / 3600
        if 0 < hours_until <= group.lead_hours:
            async with session_scope() as s:
                if await cancellations_svc.is_cancelled(s, group.id, lesson_at):
                    continue
            await _poll_group(bot, group, lesson_at)
            await _maybe_send_summary(bot, group, lesson_at, now)
            await _maybe_recruit(bot, group, lesson_at, now)


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
