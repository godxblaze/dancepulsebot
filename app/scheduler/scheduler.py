from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNotFound
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.bot import ui
from app.bot.keyboards import (
    attendance_keyboard,
    lesson_card_keyboard,
    support_ask_keyboard,
    support_pick_keyboard,
)
from app.bot.utils import format_group_title, format_place, send_with_retry
from app.core.alerts import alert_developer
from app.core.config import settings
from app.core.logging import LOG_FILE
from app.core.sentinel import ConflictSentinel
from app.db.models import (
    Admin,
    Gender,
    Group,
    RecruitMode,
    SupportCallStatus,
    SurveyKind,
    SurveyStatus,
)
from app.db.session import session_scope
from app.services import cancellations as cancellations_svc
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import place_overrides as overrides_svc
from app.services import schools as schools_svc
from app.services import supports as supports_svc
from app.services import surveys as surveys_svc
from app.services import venues as venues_svc
from app.services.schedule import (
    as_utc,
    evening_fix_at,
    format_lesson_at,
    format_weekday_long,
    next_lesson_at,
    utc_now,
)

log = logging.getLogger(__name__)

# Опрос должен уйти не позже чем за столько до вечернего разбора, чтобы к
# фиксации у людей было время ответить (иначе фаза 1 считает пустой опрос).
POLL_BEFORE_FIX = timedelta(hours=4)
# Режим escalate: если lead не разобрал pending-picker, а до занятия осталось не
# больше этого — бот авто-докидывает (open-race fallback). Используется в _maybe_recruit.
ESCALATE_LEAD = timedelta(hours=6)
# Минимальное время жизни pending-picker'а, переоткрытого фазой 2, под escalate
# (см. _maybe_recruit) — отсчитывается от lesson_at - support_cutoff_hours, не
# от ESCALATE_LEAD. Без этого зазора при дефолтных настройках (cutoff=3ч <
# ESCALATE_LEAD=6ч) свежий picker эскалировал бы на следующем же тике (~15 мин),
# не дав lead'у шанса среагировать.
ESCALATE_MIN_GRACE = timedelta(hours=1)


async def lesson_place(s, group: Group, lesson_at: datetime) -> str | None:
    """Место КОНКРЕТНОГО занятия: per-lesson override («📍 Зал на занятие» в
    карточке группы), иначе площадка+зал группы. Единая точка для опроса,
    напоминаний, реактивации и инвайтов саппортам."""
    override = await overrides_svc.get_place(s, group.id, lesson_at)
    if override:
        return override
    venue = (
        await venues_svc.get_venue(s, group.venue_id)
        if group.venue_id is not None
        else None
    )
    return format_place(group, venue)


def _answer_deadline(
    lesson_at: datetime, evening_time: str | None, now: datetime, tz: str | None
) -> str | None:
    """«HH:MM» — дедлайн ответа = время вечерней фиксации lead'а (в школьном TZ).
    None, если вечерний разбор не задан или дедлайн уже прошёл."""
    if not evening_time:
        return None
    fix = evening_fix_at(lesson_at, evening_time, tz=tz)
    if fix <= now:
        return None
    return format_lesson_at(fix, "%H:%M", tz=tz)


async def _poll_group(
    bot: Bot,
    group: Group,
    lesson_at: datetime,
    now: datetime,
    *,
    tz: str | None = None,
    evening_time: str | None = None,
) -> None:
    """Ensure the survey exists, then message everyone who hasn't been polled yet.

    Идемпотентно по пере-tick'у: tg_message_id на Response отмечает «опрос отправлен»,
    поэтому следующий tick не повторит отправку тем, кто уже получил, но дошлёт
    тем, кому не успели (новые члены / упавший send в прошлый раз).

    Если первая рассылка попадает уже внутрь 4h/1h reminder-окна — сразу метим
    reminded_*=True, чтобы _maybe_remind не пингнул того же человека повторно
    тем же tick'ом."""
    async with session_scope() as s:
        survey, _ = await surveys_svc.get_or_create_survey(
            s, group.id, SurveyKind.attendance, lesson_at
        )
        members = await persons_svc.group_members(s, group.id)
        await surveys_svc.ensure_responses(s, survey.id, [p.id for p in members])
        to_poll = await surveys_svc.persons_without_message(s, survey.id)
        coming = await surveys_svc.coming_by_gender(s, survey.id)
        survey_id = survey.id
        place = await lesson_place(s, group, lesson_at)

    if not to_poll:
        return  # all members already polled

    meta = [
        f"{ui.CALENDAR} <b>{format_weekday_long(lesson_at, tz=tz)}, "
        f"{format_lesson_at(lesson_at, '%d.%m', tz=tz)} · в {group.lesson_time}</b>",
    ]
    if group.teachers_note:
        meta.append(f"{ui.TEACHER} {escape(group.teachers_note)}")
    # B1: соц-доказательство «кто уже идёт» (до ответа). B3: дедлайн ответа.
    body_lines: list[str] = []
    coming_block = ui.format_coming(coming)
    if coming_block:
        body_lines.extend(coming_block.split("\n"))
    deadline = _answer_deadline(lesson_at, evening_time, now, tz)
    if deadline:
        body_lines.append(f"{ui.DEADLINE} ответь до {deadline}")
    # CTA отделён пустой строкой и жирным — не тонет в соц-блоке и дедлайне.
    if body_lines:
        body_lines.append("")
    body_lines.append("<b>Придёшь?</b>")
    text = ui.card(
        ui.title(ui.BRAND, escape(format_group_title(group))),
        meta=meta,
        body=body_lines,
        footer=f"{ui.PLACE} {escape(place)}" if place else None,
    )
    sent = 0
    for p in to_poll:
        try:
            msg = await send_with_retry(
                bot,
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
                # Первая доставка уже внутри reminder-окна — заранее метим
                # соответствующие флаги, чтобы _maybe_remind не дублировал.
                until = lesson_at - now
                if until <= timedelta(hours=4):
                    await surveys_svc.mark_reminded(s, survey_id, p.id, "4h")
                if until <= timedelta(hours=1):
                    await surveys_svc.mark_reminded(s, survey_id, p.id, "1h")
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


def _format_summary(
    group: Group,
    lesson_at: datetime,
    summary: surveys_svc.SurveySummary,
    *,
    coming: surveys_svc.ComingRoster | None = None,
    tz: str | None = None,
) -> str:
    body = [
        f"придут <b>{summary.yes}/{summary.total}</b>  "
        f"{ui.bar(summary.yes, summary.total)}",
    ]
    # Баланс пар + имена по полу — главный сигнал для решения о доборе.
    if coming is not None and coming.total:
        body.extend(
            ui.balance_bars(
                len(coming.male), len(coming.female),
                male_names=coming.male, female_names=coming.female,
            ).split("\n")
        )
        if coming.unknown:
            body.append(f"• {len(coming.unknown)}: {escape(', '.join(coming.unknown))}")
    # Проблемные статусы — отдельным блоком, чтобы не сливались с ростером.
    statuses: list[str] = []
    if summary.no_names:
        statuses.append(f"{ui.NO} не придут: {escape(', '.join(summary.no_names))}")
    if summary.pending_names:
        statuses.append(f"{ui.PENDING} молчат: {escape(', '.join(summary.pending_names))}")
    if summary.undelivered:
        statuses.append(
            f"{ui.UNDELIVERED} не дошло: {summary.undelivered} (не запустили бота)"
        )
    if statuses:
        body.append("")
        body.extend(statuses)
    return ui.card(
        ui.title(ui.BRAND, escape(format_group_title(group))) + " — итоги опроса",
        meta=f"занятие {format_lesson_at(lesson_at, '%d.%m', tz=tz)} в {group.lesson_time}",
        body=body,
    )


async def _maybe_send_summary(
    bot: Bot,
    group: Group,
    lesson_at: datetime,
    now: datetime,
    *,
    tz: str | None = None,
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
        coming = await surveys_svc.coming_by_gender(s, survey.id)
        admin_tg_ids = await groups_svc.group_admin_tg_ids(
            s, group.id, group.school_id
        )
        survey_id = survey.id

    if not admin_tg_ids:
        return
    text = _format_summary(group, lesson_at, summary, coming=coming, tz=tz)
    markup = lesson_card_keyboard(group.id, survey_id, summary.pending)
    delivered = 0
    terminal = 0
    for tg in admin_tg_ids:
        try:
            await send_with_retry(bot, tg, text, parse_mode=ParseMode.HTML, reply_markup=markup)
            delivered += 1
        except (TelegramForbiddenError, TelegramNotFound):
            log.info("Admin tg=%s заблокировал бота — auto-summary skipped", tg)
            terminal += 1
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to send auto-summary to admin %s: %s", tg, e)
    if delivered == 0:
        # Никто не получил. Не метим summary_sent — дошлём на следующих tick'ах.
        # Транзиентные сбои само собой ретраятся. Terminal (препод заблокировал
        # бота / ещё не нажал Start — типичный старт пилота) тоже ретраим: как
        # только препод станет доступен, сводка доедет. Окно ограничено — survey
        # закроется после занятия и этот код перестанет срабатывать.
        if terminal:
            log.warning(
                "Auto-summary survey %s: все %d админ(ов) недоступны "
                "(заблокировали бота / не нажали Start). Повторю на след. tick.",
                survey_id, terminal,
            )
        return
    # Хотя бы один админ получил — больше слать не нужно.
    async with session_scope() as s:
        await surveys_svc.mark_summary_sent(s, survey_id)
    log.info(
        "Auto-summary for survey %s sent to %d/%d admin(s)",
        survey_id, delivered, len(admin_tg_ids),
    )


def _gender_plural(gender: Gender) -> str:
    return "парни" if gender == Gender.male else "девушки"


async def send_support_invites(
    bot: Bot,
    *,
    call_id: int,
    invited: list[tuple[int, str]],
    title: str,
    place: str | None,
    teachers_note: str | None,
    lesson_time: str,
    lesson_at: datetime,
    tz: str | None,
) -> int:
    """Единый путь рассылки приглашений «нужна помощь» саппортам. Используется
    и ручным picker'ом (recruit.py), и авто-режимами open_race/auto_top.
    Собирается через ui.card + HTML — как остальные экраны Pulse; title/place/
    note приходят сырыми и экранируются здесь. Возвращает число успешно
    отправленных."""
    meta = [
        f"{ui.CALENDAR} <b>«{escape(title)}» — "
        f"{format_lesson_at(lesson_at, '%d.%m', tz=tz)} в {lesson_time}</b>",
    ]
    if teachers_note:
        meta.append(f"{ui.TEACHER} {escape(teachers_note)}")
    text = ui.card(
        ui.title(ui.SUPPORT, "Нужна помощь!"),
        meta=meta,
        body="Не хватает пары — сможешь подойти выручить?",
        footer=f"{ui.PLACE} {escape(place)}" if place else None,
    )
    sent = 0
    for tg_id, _name in invited:
        try:
            await send_with_retry(
                bot, tg_id, text,
                reply_markup=support_ask_keyboard(call_id),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Could not invite support %s: %s", tg_id, e)
            continue
        sent += 1
    return sent


async def _send_recruit_picker(
    bot: Bot,
    *,
    teacher_tgs: list[int],
    group: Group,
    lesson_at: datetime,
    call_id: int,
    gender_needed: Gender,
    deficit: int,
    male: int,
    female: int,
    candidates: list,
    place: str | None,
    tz: str | None,
    levels: dict[int, int] | None = None,
    can_expand: bool = False,
    phase2: bool = False,
    unknown: int = 0,
) -> None:
    """Шлёт lead'у группы сводку баланса + picker его пула (или «пул пуст»).
    Используется обеими фазами добора; первый разобравший выигрывает."""
    header = f"{ui.SUPPORT} Добор остатка" if phase2 else ui.SUPPORT
    head_lines = [
        f"{header} <b>{escape(format_group_title(group))}</b> · "
        f"{format_lesson_at(lesson_at, '%d.%m', tz=tz)} {group.lesson_time}",
    ]
    if place:
        head_lines.append(f"{ui.PLACE} {escape(place)}")
    head_lines.extend([
        "баланс пар:",
        ui.balance_bars(male, female),
        f"не хватает {deficit} — нужны {_gender_plural(gender_needed)}.",
    ])
    if unknown:
        # Ответившие «приду» без пола не попадают в баланс — бар может врать.
        head_lines.append(ui.no_gender_warning(unknown))
    head = "\n".join(head_lines)
    if candidates:
        body = head + "\n\nОтметь, кого позвать:"
        markup = support_pick_keyboard(
            call_id, candidates, set(), levels, can_expand=can_expand
        )
    elif can_expand:
        body = head + "\n\nВ твоём пуле нет подходящих — глянь пул коллег:"
        markup = support_pick_keyboard(call_id, [], set(), levels, can_expand=True)
    else:
        body = head + "\n\nВ пуле нет подходящих саппортов — добавь через /pool."
        markup = None
    sent_ref: tuple[int, int] | None = None
    for tg in teacher_tgs:
        try:
            msg = await send_with_retry(
                bot, tg, body, reply_markup=markup, parse_mode=ParseMode.HTML
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Could not ask admin %s about supports: %s", tg, e)
            continue
        mid = getattr(msg, "message_id", None)
        if sent_ref is None and mid is not None:
            sent_ref = (tg, mid)
    # B8: запоминаем сообщение с баланс-строкой, чтобы обновлять его при claim/skip.
    # has_keyboard: markup не None → это живой picker (кнопки выбора саппортов),
    # его текст _refresh_balance трогать не должен (см. recruit.py).
    if sent_ref is not None:
        async with session_scope() as s:
            await supports_svc.set_picker_ref(
                s, call_id, sent_ref[0], sent_ref[1], has_keyboard=markup is not None
            )


def _auto_recruit_plan(
    group: Group,
    call_id: int,
    to_invite: list,
    lead_tgs: list[int],
    gender_needed: Gender,
    deficit: int,
    male: int,
    female: int,
    *,
    kind: str,
    unknown: int = 0,
) -> dict:
    """План авто-рассылки (open_race/auto_top): кого пригласить + текст lead'у
    с баланс-строкой (B8 — обновляется в реальном времени при claim/skip).
    Пустой пул — lead получает понятный «добавь через /pool»."""
    title = escape(format_group_title(group))
    balance = ui.balance_bars(male, female)
    if not to_invite:
        lead_text = (
            f"{ui.SUPPORT} «{title}»: в пуле нет подходящих саппортов — "
            f"добавь через /pool."
        )
    elif kind == "auto_top":
        names = ", ".join(escape(p.full_name) for p in to_invite)
        lead_text = (
            f"{ui.SUPPORT} Позвал автоматически на «{title}»: {names}.\n"
            f"баланс пар:\n{balance}\nНапишу, как откликнутся."
        )
    else:  # open_race
        lead_text = (
            f"{ui.SUPPORT} Запустил открытый добор на «{title}»: нужны "
            f"{deficit} — {_gender_plural(gender_needed)}.\n"
            f"баланс пар:\n{balance}\nПервый откликнувшийся выручает."
        )
    if unknown:
        # Ответившие «приду» без пола не попадают в баланс — как в _send_recruit_picker.
        lead_text += f"\n{ui.no_gender_warning(unknown)}"
    return dict(
        call_id=call_id,
        invited=[(p.tg_user_id, p.full_name) for p in to_invite],
        lead_tgs=lead_tgs,
        lead_text=lead_text,
    )


async def _maybe_recruit(
    bot: Bot,
    group: Group,
    lesson_at: datetime,
    now: datetime,
    *,
    tz: str | None = None,
) -> None:
    """Двухфазный автодобор, привязанный к календарю (не к hours-offset):

    Фаза 1 — вечерний разбор накануне в `recruit_evening_time` (школьный TZ):
    создаём решение по занятию (нужен добор или баланс ок) и, если перекос ≥
    порога, шлём owner'у picker. Одно решение на занятие.

    Фаза 2 — добор остатка в день занятия за `support_cutoff_hours` ч (0 = выкл):
    один раз пересчитываем живой баланс с учётом уже согласившихся саппортов;
    если из-за поздних отмен появилась новая дыра — до-зываем."""
    if group.school_id is None:
        return
    send_kwargs: dict | None = None
    auto_plan: dict | None = None

    # ── Фаза 0: дешёвые reads, вне лока — решаем ветку и достаём call_id (если
    # call уже существует). Мутирующие ветки ниже re-читают call свежим внутри
    # call_lock, а не доверяют этим (потенциально устаревшим к моменту захвата
    # лока) значениям.
    async with session_scope() as s:
        # Добор ведёт «куратор» группы (lead) — его настройки, его пул, ему picker.
        lead = await groups_svc.resolve_recruit_lead(s, group)
        if lead is None:
            return
        lead_id = lead.id
        threshold = lead.support_threshold
        topup_hours = lead.support_cutoff_hours
        evening_time = lead.recruit_evening_time
        mode = lead.recruit_mode
        targets = [lead.tg_user_id]

        survey = await surveys_svc.find_attendance_survey(s, group.id, lesson_at)
        if survey is None:
            return
        survey_id = survey.id
        if survey.status != SurveyStatus.open:
            # Осиротел (правка расписания/TZ закрыла его через
            # close_orphaned_survey) — добор для него больше не ведём.
            return
        existing_call = await supports_svc.get_call_for_survey(s, survey_id)
        call_id = existing_call.id if existing_call is not None else None
        place = await lesson_place(s, group, lesson_at)

    if call_id is None:
        # ── Фаза 1: вечерний разбор (call ещё не существует — никто другой не
        # может на него ссылаться, лок не нужен) ──
        async with session_scope() as s:
            if now < evening_fix_at(lesson_at, evening_time, tz=tz):
                return
            male, female = await surveys_svc.gender_yes_counts(s, survey_id)
            deficit = abs(male - female)
            if deficit < threshold:
                await supports_svc.create_call(
                    s, survey_id, Gender.male, 0, SupportCallStatus.closed
                )
                return
            gender_needed = Gender.male if male < female else Gender.female
            if mode == RecruitMode.auto_top.value:
                # Бот сам зовёт топ-N (N=дефицит) из пула lead'а по уровню.
                new_call = await supports_svc.create_call(
                    s, survey_id, gender_needed, deficit, SupportCallStatus.open
                )
                to_invite = await persons_svc.top_candidates(
                    s, lead_id, gender_needed, group.id, deficit, group.school_id
                )
                auto_plan = _auto_recruit_plan(
                    group, new_call.id, to_invite, targets, gender_needed,
                    deficit, male, female, kind="auto_top",
                    unknown=await surveys_svc.unknown_yes_count(s, survey_id),
                )
            elif mode == RecruitMode.open_race.value:
                # Открытая гонка: приглашения сразу всему подходящему пулу.
                new_call = await supports_svc.create_call(
                    s, survey_id, gender_needed, deficit, SupportCallStatus.open
                )
                candidates = await persons_svc.support_candidates(
                    s, lead_id, gender_needed, group.id, group.school_id
                )
                colleagues = await persons_svc.school_pool_candidates(
                    s, group.school_id, gender_needed, group.id,
                    {c.id for c in candidates}, exclude_admin_id=lead_id,
                )
                auto_plan = _auto_recruit_plan(
                    group, new_call.id, candidates + colleagues, targets,
                    gender_needed, deficit, male, female, kind="open_race",
                    unknown=await surveys_svc.unknown_yes_count(s, survey_id),
                )
            else:
                # curated (дефолт) и escalate до окна эскалации — picker lead'у.
                new_call = await supports_svc.create_call(
                    s, survey_id, gender_needed, deficit, SupportCallStatus.pending
                )
                candidates = await persons_svc.support_candidates(
                    s, lead_id, gender_needed, group.id, group.school_id
                )
                levels = await persons_svc.admin_pool_levels(s, lead_id)
                colleagues = await persons_svc.school_pool_candidates(
                    s, group.school_id, gender_needed, group.id,
                    {c.id for c in candidates}, exclude_admin_id=lead_id,
                )
                send_kwargs = dict(
                    teacher_tgs=targets, call_id=new_call.id,
                    gender_needed=gender_needed, deficit=deficit, male=male,
                    female=female, candidates=candidates, place=place,
                    levels=levels, can_expand=bool(colleagues), phase2=False,
                    unknown=await surveys_svc.unknown_yes_count(s, survey_id),
                )
    else:
        # ── Call уже существует: escalate-переход и фаза-2 добора мутируют его
        # статус/поля — та же сериализация, что у tap-хендлеров recruit.py,
        # иначе тап «Не сейчас»/claim в это же мгновение может быть перезаписан
        # (см. pilot_risks — scheduler-мутации без call_lock).
        async with supports_svc.call_lock(call_id):
            async with session_scope() as s:
                call = await supports_svc.get_call(s, call_id)
                if call is None:
                    return
                if (
                    mode == RecruitMode.escalate.value
                    and call.status == SupportCallStatus.pending
                ):
                    # ── Режим escalate: human-first picker, авто-фолбэк при бездействии ──
                    # Обычный дедлайн — ESCALATE_LEAD до занятия. Но если pending — это
                    # свежий picker, который фаза 2 только что переоткрыла (topup_done),
                    # тот дедлайн при дефолтных настройках (cutoff=3ч < ESCALATE_LEAD=6ч)
                    # уже в прошлом — picker эскалировал бы на первом же тике после
                    # переоткрытия. Даём вместо этого ESCALATE_MIN_GRACE от момента,
                    # когда фаза 2 могла его переоткрыть (lesson_at − topup_hours).
                    if call.topup_done:
                        deadline = (
                            lesson_at - timedelta(hours=topup_hours) + ESCALATE_MIN_GRACE
                        )
                    else:
                        deadline = lesson_at - ESCALATE_LEAD
                    if now < deadline:
                        return  # до окна эскалации — как curated (picker уже у lead'а)
                    # lead не разобрал pending за окно до занятия → авто open-race fallback.
                    # Фильтруем уже согласившихся (по образцу фазы-2) — иначе эскалация
                    # повторно зовёт того, кто уже claim'нул этот call.
                    claimed_ids = await supports_svc.claimed_person_ids(s, call.id)
                    cands = await persons_svc.support_candidates(
                        s, lead_id, call.gender, group.id, group.school_id
                    )
                    candidates = [c for c in cands if c.id not in claimed_ids]
                    colleagues = await persons_svc.school_pool_candidates(
                        s, group.school_id, call.gender, group.id,
                        {c.id for c in candidates} | claimed_ids, exclude_admin_id=lead_id,
                    )
                    # status pending→open = гард от повторной эскалации на следующем tick'е.
                    call.status = SupportCallStatus.open
                    call.created_at = datetime.now(UTC).replace(tzinfo=None)
                    call.no_reply_notified = False
                    await s.flush()
                    esc_male, esc_female = await surveys_svc.gender_yes_counts(s, survey_id)
                    auto_plan = _auto_recruit_plan(
                        group, call.id, candidates + colleagues, targets,
                        call.gender, call.needed, esc_male, esc_female, kind="open_race",
                        unknown=await surveys_svc.unknown_yes_count(s, survey_id),
                    )
                else:
                    # ── Фаза 2: добор остатка в день занятия ──
                    if topup_hours <= 0 or call.topup_done:
                        return
                    if now < lesson_at - timedelta(hours=topup_hours):
                        return
                    call.topup_done = True
                    male, female = await surveys_svc.gender_yes_counts(s, survey_id)
                    claimed = await supports_svc.count_claims(s, call.id)
                    had_recruit = call.needed > 0
                    sec_male = claimed if had_recruit and call.gender == Gender.male else 0
                    sec_female = claimed if had_recruit and call.gender == Gender.female else 0
                    eff_male, eff_female = male + sec_male, female + sec_female
                    deficit = abs(eff_male - eff_female)
                    short = Gender.male if eff_male < eff_female else Gender.female
                    remaining = max(0, call.needed - claimed)
                    if deficit < threshold:
                        if call.status != SupportCallStatus.closed:
                            call.status = SupportCallStatus.closed  # баланс сложился
                        await s.flush()
                        return
                    if (had_recruit and short != call.gender) or deficit <= remaining:
                        # перекос ушёл в другую сторону (редко, не флипаем) либо уже
                        # добираем достаточно — ничего нового не спрашиваем.
                        await s.flush()
                        return
                    # Нужно БОЛЬШЕ `short`, чем уже добираем → расширяем и зовём ещё.
                    call.gender = short
                    call.needed = claimed + deficit
                    call.status = SupportCallStatus.pending
                    call.no_reply_notified = False
                    # Свежий picker на бо́льшую дыру — разрешаем и nudge заново, если
                    # owner и его проигнорит.
                    call.pending_notified = False
                    # сбрасываем таймер «тишины» на момент добора (иначе stale-пинг
                    # сработает сразу — call создан ещё вечером накануне).
                    call.created_at = datetime.now(UTC).replace(tzinfo=None)
                    await s.flush()
                    claimed_ids = await supports_svc.claimed_person_ids(s, call.id)
                    cands = await persons_svc.support_candidates(
                        s, lead_id, short, group.id, group.school_id
                    )
                    candidates = [c for c in cands if c.id not in claimed_ids]
                    levels = await persons_svc.admin_pool_levels(s, lead_id)
                    colleagues = await persons_svc.school_pool_candidates(
                        s, group.school_id, short, group.id,
                        {c.id for c in candidates} | claimed_ids, exclude_admin_id=lead_id,
                    )
                    send_kwargs = dict(
                        teacher_tgs=targets, call_id=call.id, gender_needed=short,
                        deficit=deficit, male=eff_male, female=eff_female,
                        candidates=candidates, place=place, levels=levels,
                        can_expand=bool(colleagues), phase2=True,
                        unknown=await surveys_svc.unknown_yes_count(s, survey_id),
                    )

    if send_kwargs is not None:
        await _send_recruit_picker(
            bot, group=group, lesson_at=lesson_at, tz=tz, **send_kwargs
        )
    if auto_plan is not None:
        if auto_plan["invited"]:
            sent = await send_support_invites(
                bot, call_id=auto_plan["call_id"], invited=auto_plan["invited"],
                title=format_group_title(group), place=place,
                teachers_note=group.teachers_note, lesson_time=group.lesson_time,
                lesson_at=lesson_at, tz=tz,
            )
            if sent == 0:
                # Все отправки инвайтов упали (Telegram недоступен/у всех
                # заблокирован бот) — не врём lead'у бодрым «позвал автоматически».
                auto_plan["lead_text"] = (
                    f"⚠️ Не удалось отправить приглашения саппортам «"
                    f"{escape(format_group_title(group))}» — попробуй сам через "
                    f"🆘 Саппорты."
                )
        lead_ref: tuple[int, int] | None = None
        for tg in auto_plan["lead_tgs"]:
            try:
                msg = await send_with_retry(
                    bot, tg, auto_plan["lead_text"], parse_mode=ParseMode.HTML
                )
            except Exception as e:  # noqa: BLE001
                log.warning("Could not send recruit heads-up to lead %s: %s", tg, e)
                continue
            mid = getattr(msg, "message_id", None)
            if lead_ref is None and mid is not None:
                lead_ref = (tg, mid)
        if lead_ref is not None:
            async with session_scope() as s:
                await supports_svc.set_picker_ref(
                    s, auto_plan["call_id"], lead_ref[0], lead_ref[1]
                )


async def _maybe_remind(
    bot: Bot,
    group: Group,
    lesson_at: datetime,
    now: datetime,
    *,
    tz: str | None = None,
) -> None:
    """Auto-reminder не-ответившим: за 4ч и за 1ч до занятия отдельные пинги.

    Окна не перекрываются: 4h-окно — (1ч, 4ч], 1h-окно — (0, 1ч]. Идемпотентность
    через Response.reminded_4h / reminded_1h."""
    until = lesson_at - now
    if timedelta() < until <= timedelta(hours=1):
        window = "1h"
    elif timedelta(hours=1) < until <= timedelta(hours=4):
        window = "4h"
    else:
        return

    async with session_scope() as s:
        survey = await surveys_svc.find_attendance_survey(s, group.id, lesson_at)
        if survey is None or survey.status != SurveyStatus.open:
            return
        to_remind = await surveys_svc.persons_to_remind(s, survey.id, window)
        survey_id = survey.id
        place = await lesson_place(s, group, lesson_at)

    if not to_remind:
        return

    meta = [
        f"{ui.CALENDAR} <b>{format_weekday_long(lesson_at, tz=tz)}, "
        f"{format_lesson_at(lesson_at, '%d.%m', tz=tz)} · в {group.lesson_time}</b>",
    ]
    if group.teachers_note:
        meta.append(f"{ui.TEACHER} {escape(group.teachers_note)}")
    # B2: commitment-копирайт — мягкий акцент на последствиях для пары.
    text = ui.card(
        ui.title(ui.REMINDER, escape(format_group_title(group))),
        meta=meta,
        body=[
            "Напоминаю, что скоро занятие.",
            ui.COMMITMENT,
            "",
            "<b>Придёшь?</b>",
        ],
        footer=f"{ui.PLACE} {escape(place)}" if place else None,
    )

    sent = 0
    for p in to_remind:
        try:
            await send_with_retry(
                bot,
                p.tg_user_id,
                text,
                reply_markup=attendance_keyboard(survey_id),
                parse_mode=ParseMode.HTML,
            )
        except (TelegramForbiddenError, TelegramNotFound):
            # Заблокировал бота / удалил аккаунт — метим как «напомнили»,
            # чтобы не долбить логи каждым tick'ом.
            async with session_scope() as s:
                await surveys_svc.mark_reminded(s, survey_id, p.id, window)
            continue
        except Exception as e:  # noqa: BLE001
            log.warning("remind: send to %s failed, will retry: %s", p.tg_user_id, e)
            continue
        async with session_scope() as s:
            await surveys_svc.mark_reminded(s, survey_id, p.id, window)
        sent += 1
    if sent:
        log.info(
            "Survey %s: %s-reminder delivered %d/%d to silent members",
            survey_id, window, sent, len(to_remind),
        )


SUPPORT_NO_REPLY_AFTER = timedelta(hours=1)
# Pending-call (owner получил picker, но не нажал «Позвать») напоминаем, когда
# до занятия осталось не больше этого.
PENDING_NUDGE_LEAD = timedelta(hours=12)
# B5 реактивация: пропуск последних N занятий → мягкий пинг, не чаще раза в окно.
REACTIVATE_MISSES = 2
REACTIVATE_THROTTLE = timedelta(days=14)


async def _maybe_reactivate(
    bot: Bot,
    group: Group,
    lesson_at: datetime,
    now: datetime,
    *,
    tz: str | None = None,
) -> None:
    """B5: мягкий персональный пинг тем, кто пропустил последние
    `REACTIVATE_MISSES` занятий. Throttle — не чаще раза в `REACTIVATE_THROTTLE`.
    Не трогает тех, кто уже ответил на ближайший опрос."""
    if group.school_id is None:
        return
    now_naive = (
        now.replace(tzinfo=None) if now.tzinfo is None
        else now.astimezone(UTC).replace(tzinfo=None)
    )
    cutoff = now_naive - REACTIVATE_THROTTLE
    async with session_scope() as s:
        survey = await surveys_svc.find_attendance_survey(s, group.id, lesson_at)
        if survey is None:
            return
        survey_id = survey.id
        lapsed = await surveys_svc.lapsed_members(s, group.id, k=REACTIVATE_MISSES)
        pending_ids = {p.id for p in await surveys_svc.pending_persons(s, survey_id)}
        place = await lesson_place(s, group, lesson_at)
        targets = [
            (p.tg_user_id, p.id)
            for p in lapsed
            if p.id in pending_ids
            and (p.last_reactivation_at is None or p.last_reactivation_at <= cutoff)
        ]

    if not targets:
        return
    meta = [
        f"{ui.CALENDAR} <b>{format_weekday_long(lesson_at, tz=tz)}, "
        f"{format_lesson_at(lesson_at, '%d.%m', tz=tz)} · в {group.lesson_time}</b>",
    ]
    text = ui.card(
        ui.title(ui.BRAND, escape(format_group_title(group))),
        meta=meta,
        body=[
            "Давно тебя не было — соскучились!",
            ui.COMMITMENT,
            "",
            "<b>Придёшь?</b>",
        ],
        footer=f"{ui.PLACE} {escape(place)}" if place else None,
    )
    for tg_id, pid in targets:
        try:
            await send_with_retry(
                bot, tg_id, text,
                reply_markup=attendance_keyboard(survey_id),
                parse_mode=ParseMode.HTML,
            )
        except (TelegramForbiddenError, TelegramNotFound):
            # Заблокировал/недоступен — всё равно помечаем, чтобы не долбить.
            async with session_scope() as s:
                await persons_svc.mark_reactivated(s, pid, now_naive)
            continue
        except Exception as e:  # noqa: BLE001
            log.warning("reactivate: send to %s failed, will retry: %s", tg_id, e)
            continue
        async with session_scope() as s:
            await persons_svc.mark_reactivated(s, pid, now_naive)


async def _stale_call_context(s, call, *, survey=None):
    """(admin_tg_ids, title) для SupportCall — или None если чего-то нет."""
    if survey is None:
        survey = await surveys_svc.get_survey(s, call.survey_id)
    if survey is None:
        return None
    group = await groups_svc.get_group(s, survey.group_id)
    if group is None or group.school_id is None:
        return None
    admin_tgs = await groups_svc.group_admin_tg_ids(s, group.id, group.school_id)
    if not admin_tgs:
        return None
    return admin_tgs, format_group_title(group)


async def _notify_stale_support_calls(bot: Bot, now: datetime) -> None:
    """Два пинга preподу:
    - open-call без достаточного числа откликов >SUPPORT_NO_REPLY_AFTER —
      «никто не откликнулся, попробуй сам»;
    - pending-call, который owner так и не разобрал, а занятие уже близко —
      «разбери добор»."""
    # (call_id, tg, text) — флаг notified ставим ПОСЛЕ реальной отправки (см.
    # ниже), а не до неё: иначе сбой send_message («никто не откликнулся» не
    # дошло) навсегда съедает этот единственный пинг без ретрая.
    no_reply_msgs: list[tuple[int, int, str]] = []
    pending_msgs: list[tuple[int, int, str]] = []
    async with session_scope() as s:
        # SupportCall.created_at в SQLite — naive UTC (server_default=now()),
        # потому сравниваем naive vs naive.
        now_utc = datetime.now(UTC).replace(tzinfo=None)
        for call in await supports_svc.stale_open_calls(
            s, now_utc, SUPPORT_NO_REPLY_AFTER
        ):
            filled = await supports_svc.count_claims(s, call.id)
            if filled >= call.needed:
                await supports_svc.mark_no_reply_notified(s, call.id)
                continue
            survey = await surveys_svc.get_survey(s, call.survey_id)
            if survey is not None and as_utc(survey.lesson_at) <= now:
                continue  # занятие уже началось/прошло — поздно «напиши им лично»
            ctx = await _stale_call_context(s, call, survey=survey)
            if ctx is None:
                continue
            admin_tgs, title = ctx
            for tg in admin_tgs:
                no_reply_msgs.append((
                    call.id, tg,
                    f"🆘 «{escape(title)}» — никто из приглашённых не откликнулся "
                    f"за час ({filled}/{call.needed}). Стоит написать им лично или "
                    f"поискать саппорта через /pool.",
                ))

        for call in await supports_svc.pending_unnotified_calls(s):
            survey = await surveys_svc.get_survey(s, call.survey_id)
            if survey is None:
                continue
            lesson_at = as_utc(survey.lesson_at)
            if now >= lesson_at or lesson_at - now > PENDING_NUDGE_LEAD:
                continue  # занятие прошло либо ещё рано напоминать
            ctx = await _stale_call_context(s, call, survey=survey)
            if ctx is None:
                continue
            admin_tgs, title = ctx
            for tg in admin_tgs:
                pending_msgs.append((
                    call.id, tg,
                    f"🆘 «{escape(title)}» — добор саппортов не разобран, "
                    f"а занятие уже скоро. Загляни в сообщение выше или в /school.",
                ))

    sent_call_ids: set[int] = set()
    for call_id, tg, text in no_reply_msgs + pending_msgs:
        try:
            await send_with_retry(bot, tg, text, parse_mode=ParseMode.HTML)
            sent_call_ids.add(call_id)
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to send support notice to admin %s: %s", tg, e)

    if sent_call_ids:
        async with session_scope() as s:
            for call_id in {c for c, _, _ in no_reply_msgs} & sent_call_ids:
                await supports_svc.mark_no_reply_notified(s, call_id)
            for call_id in {c for c, _, _ in pending_msgs} & sent_call_ids:
                await supports_svc.mark_pending_notified(s, call_id)


async def tick(bot: Bot) -> None:
    """Runs every 15 min: close past surveys, send polls, auto-summaries and recruit."""
    now = utc_now()
    async with session_scope() as s:
        closed = await surveys_svc.close_past_surveys(s, now)
    if closed:
        log.info("Closed %d past survey(s)", closed)

    await _notify_stale_support_calls(bot, now)

    async with session_scope() as s:
        from app.services import admins as admins_svc
        groups = await groups_svc.active_groups(s)
        # tz — по школе; вечернее время разбора — личное у lead'а группы (кэш
        # по admin_id, чтобы не дёргать одного и того же препода многократно).
        tz_cache: dict[int, str] = {}
        admin_evening: dict[int, str | None] = {}
        group_evening: dict[int, str | None] = {}
        for group in groups:
            if group.school_id is None:
                group_evening[group.id] = None
                continue
            if group.school_id not in tz_cache:
                tz_cache[group.school_id] = await schools_svc.get_timezone(
                    s, group.school_id
                )
            lead_id = group.recruit_lead_admin_id
            if lead_id is None:
                owner_tg = await schools_svc.get_owner_tg_id(s, group.school_id)
                owner = await admins_svc.get_by_tg_id(s, owner_tg) if owner_tg else None
                lead_id = owner.id if owner else None
            if lead_id is None:
                group_evening[group.id] = None
                continue
            if lead_id not in admin_evening:
                lead = await s.get(Admin, lead_id)
                admin_evening[lead_id] = lead.recruit_evening_time if lead else None
            group_evening[group.id] = admin_evening[lead_id]

    for group in groups:
        # Изоляция по группам: сбой в одной (битые данные, None lead, упавший
        # send) не должен лишать остальные группы этого tick'а. Логируем и идём
        # дальше — иначе одна проблемная группа молча стопорит весь tick каждые
        # 15 минут.
        try:
            tz = tz_cache.get(group.school_id) if group.school_id else None
            lesson_at = next_lesson_at(group.weekdays, group.lesson_time, now, tz=tz)
            if lesson_at is None or now >= lesson_at:
                continue
            # Окно активности группы открывается за lead_hours до занятия, но если у
            # lead'а включён вечерний разбор — не позже чем за POLL_BEFORE_FIX до него,
            # чтобы к фиксации опрос уже разошёлся и были ответы.
            window_start = lesson_at - timedelta(hours=group.lead_hours)
            evening_time = group_evening.get(group.id)
            if evening_time:
                window_start = min(
                    window_start,
                    evening_fix_at(lesson_at, evening_time, tz=tz) - POLL_BEFORE_FIX,
                )
            if window_start <= now:
                async with session_scope() as s:
                    if await cancellations_svc.is_cancelled(s, group.id, lesson_at):
                        continue
                # reactivate ДО poll: иначе только что разосланный «придёшь?»
                # свежего survey тут же считается pending → лапсед-член получает
                # «давно не видели» секундами позже своего же обычного опроса.
                # До создания survey этого занятия find_attendance_survey вернёт
                # None и функция тихо выйдет — нудж просто уедет на следующий тик.
                await _maybe_reactivate(bot, group, lesson_at, now, tz=tz)
                await _poll_group(
                    bot, group, lesson_at, now, tz=tz, evening_time=evening_time
                )
                await _maybe_send_summary(bot, group, lesson_at, now, tz=tz)
                await _maybe_remind(bot, group, lesson_at, now, tz=tz)
                await _maybe_recruit(bot, group, lesson_at, now, tz=tz)
        except Exception:  # noqa: BLE001
            log.exception(
                "tick: группа «%s» (id=%s) упала — пропускаю до след. tick'а",
                group.name, group.id,
            )
            continue


async def _conflict_sentinel_tick(bot: Bot, sentinel: ConflictSentinel) -> None:
    async def _alert(text: str) -> None:
        await alert_developer(bot, text)

    await sentinel.check(datetime.now(UTC), _alert)


def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=settings.timezone)
    sched.add_job(
        tick,
        "interval",
        minutes=15,
        args=[bot],
        id="attendance_tick",
        next_run_time=datetime.now(ZoneInfo(settings.timezone)),
        # Гигиена: долгий tick (большой фан-аут / rate-limit) не должен молча
        # терять следующий запуск (misfire_grace_time), запускаться параллельно
        # сам с собой (max_instances=1) или копить пропущенные прогоны (coalesce).
        misfire_grace_time=300,
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        _conflict_sentinel_tick,
        "interval",
        minutes=5,
        args=[bot, ConflictSentinel(LOG_FILE)],
        id="conflict_sentinel",
        misfire_grace_time=300,
        max_instances=1,
        coalesce=True,
    )
    log.info("Scheduler built — attendance tick every 15 min, conflict sentinel every 5 min")
    return sched
