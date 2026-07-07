from __future__ import annotations

import asyncio
import logging
import secrets
import weakref
from datetime import UTC, datetime, timedelta
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram_dialog import DialogManager
from sqlalchemy import case, func, select

from app.bot import ui
from app.bot.commands import grant_admin_commands, revoke_admin_commands
from app.bot.handlers._access import (
    accessible_group as _accessible_group,
)
from app.bot.handlers._access import (
    group_card_extras as _group_card_extras,
)
from app.bot.handlers._access import (
    is_owner_of_group_id as _is_owner_of_group_id,
)
from app.bot.handlers._access import (
    owned_group as _owned_group,
)
from app.bot.handlers.student import show_student_home, show_student_today
from app.bot.keyboards import (
    MENU_EXPORT,
    MENU_GROUPS,
    MENU_HELP,
    MENU_NAME,
    MENU_NEWGROUP,
    MENU_STATS,
    MENU_TODAY,
    admin_menu_keyboard,
    attendance_keyboard,
    confirm_delete_keyboard,
    confirm_remove_from_group_keyboard,
    gender_from_code,
    group_actions_keyboard,
    group_admins_keyboard,
    groups_keyboard,
    lvl_from_code,
    person_card_keyboard,
    stats_keyboard,
    students_keyboard,
    today_ping_keyboard,
)
from app.bot.utils import (
    format_group_title,
    get_bot_username,
    qr_png,
)
from app.core.config import settings
from app.db.models import (
    Answer,
    Gender,
    Group,
    Person,
    Response,
    School,
    SchoolMember,
    SupportCall,
    SupportCallStatus,
    Survey,
    SurveyStatus,
)
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import exports as exports_svc
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import schools as schools_svc
from app.services import surveys as surveys_svc
from app.services import venues as venues_svc
from app.services.schedule import (
    format_lesson_at,
    format_weekdays,
    next_lesson_at,
    utc_now,
)

log = logging.getLogger(__name__)
router = Router()

_MAX_LEVEL = 3

_HELP_ADMIN = (
    "💓 Dance Pulse — бот опросов посещаемости\n\n"
    "Перед каждым занятием бот спрашивает учеников группы, придут ли они, "
    "и показывает тебе сводку.\n\n"
    "Команды:\n"
    "/newgroup — создать группу\n"
    "/groups — мои группы\n"
    "/today — сводка по ближайшим занятиям\n"
    "/stats — посещаемость, топ саппортов, давно не приходили\n"
    "/pool — пул саппортов на подмену\n"
    "/export — выгрузить учеников и посещаемость CSV-ками\n"
    "/name — изменить, как тебя показывать\n"
    "/help — эта справка"
)

_HELP_STUDENT = (
    "💓 Dance Pulse\n\n"
    "Перед каждым занятием я спрошу, придёшь ли ты — просто жми кнопку в личке.\n\n"
    "Команды:\n"
    "/groups — мои группы\n"
    "/today — что у меня сегодня/завтра\n"
    "/name — изменить, как тебя показывать"
)


async def _join_link(bot: Bot, code: str) -> str:
    username = await get_bot_username(bot)
    return f"https://t.me/{username}?start=join_{code}"


async def _with_admin_school(session, admin, call, empty):
    """Resolve the admin's primary school (owned, else first membership —
    иначе co-admin без owned-школы получал b""/[] на /export и статистике),
    then `await call(school_id)`. If the admin is in no school, return `empty`."""
    school_id = await schools_svc.primary_school_id(session, admin.id)
    if school_id is None:
        return empty
    return await call(school_id)


async def _top_supports_for(session, admin, limit: int = 3) -> list:
    return await _with_admin_school(
        session, admin, lambda sid: persons_svc.top_supports(session, sid, limit=limit), []
    )


async def _long_absent_for(session, admin, since) -> list:
    return await _with_admin_school(
        session, admin, lambda sid: persons_svc.long_absent(session, sid, since), []
    )


async def _students_csv_for(session, admin) -> bytes:
    return await _with_admin_school(
        session, admin, lambda sid: exports_svc.students_csv(session, sid), b""
    )


async def _attendance_csv_for(session, admin) -> bytes:
    return await _with_admin_school(
        session, admin, lambda sid: exports_svc.attendance_csv(session, sid), b""
    )


# ── registration & help ──────────────────────────────────────────────


@router.message(CommandStart())
async def cmd_start(message: Message, dialog_manager: DialogManager) -> None:
    tg = message.from_user
    if not tg:
        return
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, tg.id)
        if teacher is None and tg.id == settings.developer_id:
            teacher = await admins_svc.get_or_create(
                s, tg.id, tg.username, tg.full_name or f"id{tg.id}"
            )
        admin_schools: list = []
        completed_setup = False
        if teacher is not None:
            admin_schools = await schools_svc.admin_schools(s, teacher.id)
            completed_setup = bool(teacher.has_completed_setup)
    if teacher is None:
        # everyone is a student by default — teacher status is granted via /grant
        await show_student_home(message)
        return
    if dialog_manager.has_context():
        # /start — безопасная гавань: закрыть любой зависший wizard (напр.
        # брошенный на середине /newgroup), чтобы он не пережил рестарт бота
        # и не перехватывал дальнейшие тапы по нижнему меню (см. ClearStaleCancelState
        # для аналогичного случая с классическим FSM). remove_keyboard=False —
        # старое сообщение мастера могло устареть (>48ч) и editMessageReplyMarkup
        # на нём упал бы; сам wizard всё равно не актуален, лишняя клавиатура не страшна.
        try:
            await dialog_manager.reset_stack(remove_keyboard=False)
        except Exception as e:  # noqa: BLE001
            log.warning("reset_stack on /start failed for %s: %s", tg.id, e)
    if not admin_schools and not completed_setup:
        # Свежий admin: только что получил /grant или первый /start developer'а.
        # Первый шаг — назвать собственную школу через CreateSchool dialog.
        from aiogram_dialog import StartMode

        from app.bot.dialogs import CreateSchoolSG
        await dialog_manager.start(CreateSchoolSG.name, mode=StartMode.RESET_STACK)
        return
    if not admin_schools:
        # Бывший co-admin кого выгнали (или сам вышел) из всех школ. Чистим
        # Admin-запись, чтобы повторное приглашение прошло онбординг с нуля,
        # и снимаем admin-меню на стороне Telegram.
        async with session_scope() as s:
            await admins_svc.delete_admin(s, teacher.id)
        try:
            await revoke_admin_commands(message.bot, tg.id)
        except Exception as e:  # noqa: BLE001
            log.warning("revoke admin cmds for %s failed: %s", tg.id, e)
        await show_student_home(message)
        return
    await message.answer(
        "💓 Dance Pulse\n\n"
        "Я опрашиваю учеников перед каждым занятием и сам собираю саппортов, "
        "когда в паре перекос.\n\n"
        "Всё — кнопками меню снизу 👇",
        reply_markup=admin_menu_keyboard(),
    )


@router.message(Command("help"))
@router.message(F.text == MENU_HELP)
async def cmd_help(message: Message) -> None:
    tg = message.from_user
    if not tg:
        return
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, tg.id)
    await message.answer(_HELP_ADMIN if teacher else _HELP_STUDENT)


@router.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    if message.from_user:
        await message.answer(f"Твой Telegram ID: {message.from_user.id}")


@router.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject, bot: Bot) -> None:
    """Developer-only — grant teacher status to a telegram id."""
    tg = message.from_user
    if not tg or tg.id != settings.developer_id:
        return  # only the developer; silent for everyone else
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Использование: /grant <id> — число (узнать: /myid).")
        return
    target_id = int(arg)
    async with session_scope() as s:
        if await admins_svc.get_by_tg_id(s, target_id) is not None:
            await message.answer(f"id {target_id} — уже преподаватель.")
            return
        await admins_svc.get_or_create(s, target_id, None, f"id{target_id}")
    try:
        await grant_admin_commands(bot, target_id)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not set teacher commands for %s: %s", target_id, e)
    await message.answer(f"✅ id {target_id} теперь преподаватель.")
    try:
        await bot.send_message(
            target_id,
            "✅ Тебе выдан статус админа.\n\n"
            "Открой бот командой /start — я попрошу тебя дать название "
            "твоей школе, и можно работать.",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Could not notify new admin %s: %s", target_id, e)


@router.message(Command("claim"))
async def cmd_claim(message: Message, command: CommandObject, bot: Bot) -> None:
    """Self-serve онбординг препода по секрету из .env (OWNER_CLAIM_SECRET).
    Снимает ручной /grant разработчиком на пилоте: владелец школы пишет боту
    `/claim <секрет>` и сразу получает доступ."""
    tg = message.from_user
    secret = settings.owner_claim_secret
    if not tg or not secret:
        return  # фича выключена — молчим, не палим существование команды
    arg = (command.args or "").strip()
    if not secrets.compare_digest(arg, secret):
        await message.answer("Неверный код. Возьми актуальный у администратора бота.")
        return
    async with session_scope() as s:
        if await admins_svc.get_by_tg_id(s, tg.id) is not None:
            await message.answer("Ты уже преподаватель — нажми /start.")
            return
        await admins_svc.get_or_create(
            s, tg.id, tg.username, tg.full_name or f"id{tg.id}"
        )
    try:
        await grant_admin_commands(bot, tg.id)
    except Exception as e:  # noqa: BLE001
        log.warning("claim: could not set teacher commands for %s: %s", tg.id, e)
    await message.answer(
        "✅ Готово, ты преподаватель!\n\n"
        "Нажми /start — я попрошу название твоей школы, и можно работать."
    )


async def _health_snapshot(s) -> dict[str, int]:
    """Быстрый DB-хелсчек для /status разработчика."""
    async def _count(model, *where) -> int:
        stmt = select(func.count()).select_from(model)
        for w in where:
            stmt = stmt.where(w)
        return (await s.execute(stmt)).scalar_one()

    return {
        "schools": await _count(School),
        "groups": await _count(Group, Group.active.is_(True)),
        "persons": await _count(Person),
        "open_surveys": await _count(Survey, Survey.status == SurveyStatus.open),
        "pending_calls": await _count(
            SupportCall, SupportCall.status == SupportCallStatus.pending
        ),
        "open_calls": await _count(
            SupportCall, SupportCall.status == SupportCallStatus.open
        ),
    }


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    """DB-хелсчек для разработчика (settings.developer_id): жив ли бот и его
    масштаб. Не в публичном меню — для всех прочих молчим."""
    tg = message.from_user
    if tg is None or settings.developer_id <= 0 or tg.id != settings.developer_id:
        return
    async with session_scope() as s:
        snap = await _health_snapshot(s)
    await message.answer(
        "🩺 <b>Bot OK</b>\n"
        f"школ: {snap['schools']} · активных групп: {snap['groups']}\n"
        f"учеников: {snap['persons']} · открытых опросов: {snap['open_surveys']}\n"
        f"добор pending/open: {snap['pending_calls']}/{snap['open_calls']}",
        parse_mode=ParseMode.HTML,
    )


async def _devstats_snapshot(s) -> list[dict]:
    """Per-school разрез вовлечённости за последние 4 недели для /devstats —
    в отличие от /status (плоские суммы по всей базе), тут видно, какая школа
    реально живёт, а какая заглохла."""
    since = utc_now() - timedelta(weeks=4)
    schools = (await s.execute(select(School).order_by(School.id))).scalars().all()
    rows: list[dict] = []
    for school in schools:
        n_admins = (await s.execute(
            select(func.count()).select_from(SchoolMember)
            .where(SchoolMember.school_id == school.id)
        )).scalar_one()
        n_groups = (await s.execute(
            select(func.count()).select_from(Group)
            .where(Group.school_id == school.id, Group.active.is_(True))
        )).scalar_one()
        last_activity = (await s.execute(
            select(func.max(Survey.created_at))
            .select_from(Survey)
            .join(Group, Survey.group_id == Group.id)
            .where(Group.school_id == school.id)
        )).scalar_one()
        total, yes, marked, attended = (await s.execute(
            select(
                func.count(Response.id),
                func.sum(case((Response.answer == Answer.yes, 1), else_=0)),
                func.sum(case((Response.actually_attended.is_not(None), 1), else_=0)),
                func.sum(case((Response.actually_attended.is_(True), 1), else_=0)),
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
        stuck_calls = (await s.execute(
            select(func.count())
            .select_from(SupportCall)
            .join(Survey, SupportCall.survey_id == Survey.id)
            .join(Group, Survey.group_id == Group.id)
            .where(
                Group.school_id == school.id,
                Survey.lesson_at >= since,
                SupportCall.status.in_(
                    (SupportCallStatus.pending, SupportCallStatus.open)
                ),
            )
        )).scalar_one()
        rows.append({
            "name": school.name,
            "admins": n_admins,
            "groups": n_groups,
            "last_activity": last_activity,
            "responded": total or 0,
            "yes": yes or 0,
            "marked": marked or 0,
            "attended": attended or 0,
            "stuck_calls": stuck_calls,
        })
    rows.sort(
        key=lambda r: r["last_activity"] or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return rows


@router.message(Command("devstats"))
async def cmd_devstats(message: Message) -> None:
    """Разработчику: кто из школ реально пользуется ботом и с каким успехом
    (отклик на опросы, реальная посещаемость, зависший добор), за 4 недели.
    Дополняет /status — тот же гейт по settings.developer_id."""
    tg = message.from_user
    if tg is None or settings.developer_id <= 0 or tg.id != settings.developer_id:
        return
    async with session_scope() as s:
        rows = await _devstats_snapshot(s)
    if not rows:
        await message.answer("Школ пока нет.")
        return
    blocks: list[str] = []
    for r in rows:
        last = f"{r['last_activity']:%d.%m.%Y}" if r["last_activity"] else "никогда"
        lines = [
            f"{ui.title(ui.STATS, escape(r['name']))} — "
            f"{r['admins']} {ui.plural(r['admins'], 'админ', 'админа', 'админов')}, "
            f"{r['groups']} {ui.plural(r['groups'], 'группа', 'группы', 'групп')}",
            f"последняя активность: {last}",
        ]
        if r["responded"]:
            pct = round(100 * r["yes"] / r["responded"])
            lines.append(f"отклик за 4 нед.: <b>{pct}%</b> ({r['responded']} ответов)")
            if r["marked"]:
                pct_actual = round(100 * r["attended"] / r["marked"])
                lines.append(
                    f"реальная посещаемость: <b>{pct_actual}%</b> "
                    f"(по {r['marked']} отмеченным)"
                )
        else:
            lines.append("отклик за 4 нед.: нет данных")
        if r["stuck_calls"]:
            lines.append(f"{ui.SLEEP} зависший добор: {r['stuck_calls']}")
        blocks.append("\n".join(lines))
    await message.answer(
        f"{ui.title(ui.TROPHY, 'Dev stats — по школам')}\n\n" + "\n\n".join(blocks),
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("today"))
@router.message(F.text == MENU_TODAY)
async def cmd_today(message: Message) -> None:
    tg = message.from_user
    if not tg:
        return
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, tg.id)
    if teacher is None:
        await show_student_today(message)
        return
    text, markup = await _build_today_view(teacher.id, span="day")
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=markup)


@router.callback_query(F.data.startswith("today:"))
async def cb_today_switch(call: CallbackQuery) -> None:
    """Переключение между «сегодня+завтра» и «на неделю» для /today."""
    span = (call.data or "").split(":", 1)[1]
    if span not in ("day", "week"):
        await call.answer()
        return
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
    if teacher is None:
        await call.answer()
        return
    text, markup = await _build_today_view(teacher.id, span=span)
    if call.message:
        try:
            await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as e:  # noqa: BLE001
            log.debug("today_switch edit_text: %s", e)
    await call.answer()


async def _build_today_view(teacher_id: int, span: str):
    """Возвращает (text, markup) для /today. span ∈ {'day', 'week'}.

    Date-сравнения для «сегодня/завтра» делаются в TZ каждой школы — иначе
    при разнице UTC vs местного дня (например, 23:00 МСК = 20:00 UTC) дни
    могут разъехаться."""
    from app.services.schedule import school_tz_zone

    now = utc_now()
    horizon_days = 7 if span == "week" else 2
    blocks: list[str] = []
    pending_by_group: list[tuple[str, int, int]] = []

    async with session_scope() as s:
        groups = await groups_svc.admin_visible_groups(s, teacher_id)
        tz_cache: dict[int, str] = {}
        for g in groups:
            if g.school_id is not None and g.school_id not in tz_cache:
                tz_cache[g.school_id] = await schools_svc.get_timezone(s, g.school_id)
        for g in groups:
            tz_name = tz_cache.get(g.school_id) if g.school_id else None
            tz = school_tz_zone(tz_name)
            today_local = now.astimezone(tz).date()
            horizon_dates = {today_local + timedelta(days=i) for i in range(horizon_days)}
            lesson_at = next_lesson_at(g.weekdays, g.lesson_time, now, tz=tz_name)
            if lesson_at is None:
                continue
            lesson_local_date = lesson_at.astimezone(tz).date()
            if lesson_local_date not in horizon_dates:
                continue
            if lesson_local_date == today_local:
                when = "сегодня"
            elif lesson_local_date == today_local + timedelta(days=1):
                when = "завтра"
            else:
                when = format_lesson_at(lesson_at, "%d.%m (%a)", tz=tz_name)
            survey = await surveys_svc.find_attendance_survey(s, g.id, lesson_at)
            head = f"{ui.title(ui.PAIR, escape(format_group_title(g)))} · {when} {g.lesson_time}"
            if survey is None:
                blocks.append(f"{head}\nопрос ещё не разослан")
                continue
            summary = await surveys_svc.summarize(s, survey.id)
            pct = round(100 * summary.yes / summary.total) if summary.total else 0
            lines = [
                head,
                f"придут <b>{summary.yes}/{summary.total}</b> ({pct}%)  "
                f"{ui.bar(summary.yes, summary.total)}",
            ]
            if summary.no_names:
                lines.append(f"{ui.NO} {escape(', '.join(summary.no_names))}")
            if summary.pending_names:
                lines.append(f"{ui.PENDING} {escape(', '.join(summary.pending_names))}")
            if summary.undelivered:
                lines.append(
                    f"{ui.UNDELIVERED} не дошло: {summary.undelivered} (не запустили бота)"
                )
            blocks.append("\n".join(lines))
            if summary.pending > 0:
                pending_by_group.append((g.name, survey.id, summary.pending))

    if not blocks:
        empty = "На неделе занятий нет." if span == "week" else "На сегодня и завтра занятий нет."
        return empty, today_ping_keyboard([], span=span)
    return "\n\n".join(blocks), today_ping_keyboard(pending_by_group, span=span)


async def _send_export_files(message: Message, teacher) -> None:
    async with session_scope() as s:
        students_data = await _students_csv_for(s, teacher)
        attendance_data = await _attendance_csv_for(s, teacher)
    await message.answer_document(
        BufferedInputFile(students_data, "students.csv"),
        caption="📥 Ученики и пул саппортов (UTF-8 + BOM, открывается Excel'ем).",
    )
    await message.answer_document(
        BufferedInputFile(attendance_data, "attendance.csv"),
        caption="📥 Посещаемость по всем занятиям: обещание vs реально.",
    )


@router.message(Command("export"))
@router.message(F.text == MENU_EXPORT)
async def cmd_export(message: Message, bot: Bot) -> None:
    """CSV-выгрузка учеников и посещаемости. Доступна и кнопкой в /stats."""
    tg = message.from_user
    if not tg:
        return
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, tg.id)
    if teacher is None:
        return  # not an admin — silent
    await _send_export_files(message, teacher)


@router.callback_query(F.data == "statsexport")
async def cb_stats_export(call: CallbackQuery) -> None:
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
    if teacher is None or call.message is None:
        await call.answer()
        return
    await call.answer("Готовлю CSV…")
    await _send_export_files(call.message, teacher)


async def _build_stats_text(teacher, scope: str) -> str | None:
    """Текст статистики. scope: 'school' — все группы школ админа, 'mine' —
    только где он ответственный. None — если групп в этом разрезе нет."""
    blocks: list[str] = []
    async with session_scope() as s:
        groups = (
            await groups_svc.my_groups(s, teacher.id)
            if scope == "mine"
            else await groups_svc.admin_visible_groups(s, teacher.id)
        )
        if not groups:
            return None
        top_helpers = await _top_supports_for(s, teacher, limit=3)
        four_weeks_ago = utc_now() - timedelta(weeks=4)
        absent = await _long_absent_for(s, teacher, four_weeks_ago)
        for g in groups:
            closed = await surveys_svc.recent_closed_surveys(s, g.id, limit=5)
            if not closed:
                blocks.append(
                    f"{ui.title(ui.STATS, escape(format_group_title(g)))}"
                    " — пока нет завершённых опросов"
                )
                continue
            tz_name = (
                await schools_svc.get_timezone(s, g.school_id) if g.school_id else None
            )
            n_closed = len(closed)
            adj = ui.plural(n_closed, "последнее", "последние", "последние")
            word = ui.plural(n_closed, "занятие", "занятия", "занятий")
            lines = [
                f"{ui.title(ui.STATS, escape(format_group_title(g)))} — "
                f"{adj} {n_closed} {word}:"
            ]
            total_yes = total_all = 0
            total_actual_attended = total_actual_marked = 0
            for survey in reversed(closed):  # oldest first
                summary = await surveys_svc.summarize(s, survey.id)
                total_yes += summary.yes
                total_all += summary.total
                when_str = format_lesson_at(survey.lesson_at, "%d.%m", tz=tz_name)
                line = (
                    f"{ui.bar(summary.yes, summary.total)}  "
                    f"{when_str} · обещали {summary.yes}/{summary.total}"
                )
                if summary.actual_attended is not None:
                    # «обещали M → пришли N (no-show: K)»
                    no_show = max(0, summary.yes - summary.actual_attended)
                    line += f" → пришли {summary.actual_attended}"
                    if no_show:
                        line += f" (no-show: {no_show})"
                    total_actual_attended += summary.actual_attended
                    total_actual_marked += summary.actual_marked
                lines.append(line)
            if total_all:
                pct_promised = round(100 * total_yes / total_all)
                lines.append(f"средний отклик (обещали): <b>{pct_promised}%</b>")
                if total_actual_marked:
                    pct_actual = round(100 * total_actual_attended / total_actual_marked)
                    lines.append(
                        f"реальная посещаемость: <b>{pct_actual}%</b> "
                        f"(по {total_actual_marked} отмеченным)"
                    )
            blocks.append("\n".join(lines))

    if top_helpers:
        top_lines = [f"{ui.TROPHY} <b>Топ саппортов</b>"]
        for p in top_helpers:
            top_lines.append(f"• {escape(p.full_name)} — помог {p.helped_count}")
        blocks.append("\n".join(top_lines))

    if absent:
        ab_lines = [f"{ui.SLEEP} <b>Давно не приходили</b> (≥4 недель или вообще ни разу)"]
        for person, last in absent[:10]:
            tail = f"последний раз {last:%d.%m.%Y}" if last else "ни разу не отмечен"
            ab_lines.append(f"• {escape(person.full_name)} — {tail}")
        if len(absent) > 10:
            ab_lines.append(f"… и ещё {len(absent) - 10}")
        blocks.append("\n".join(ab_lines))

    header = (
        f"{ui.STATS} Вся школа" if scope == "school" else f"{ui.STATS} Только мои группы"
    )
    return f"<b>{header}</b>\n\n" + "\n\n".join(blocks)


@router.message(Command("stats"))
@router.message(F.text == MENU_STATS)
async def cmd_stats(message: Message) -> None:
    tg = message.from_user
    if not tg:
        return
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, tg.id)
    if teacher is None:
        return
    text = await _build_stats_text(teacher, "school")
    if text is None:
        await message.answer(
            "В школе пока нет групп. Создай первую: 🏫 Школа → ➕ Новая группа."
        )
        return
    await message.answer(
        text, parse_mode=ParseMode.HTML, reply_markup=stats_keyboard("school")
    )


@router.callback_query(F.data.startswith("statsscope:"))
async def cb_stats_scope(call: CallbackQuery) -> None:
    scope = (call.data or "").split(":", 1)[1]
    if scope not in ("school", "mine"):
        await call.answer()
        return
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
    if teacher is None or call.message is None:
        await call.answer()
        return
    text = await _build_stats_text(teacher, scope)
    if text is None:
        await call.answer(
            "В этом разрезе групп нет" if scope == "mine" else "Групп нет",
            show_alert=True,
        )
        return
    try:
        await call.message.edit_text(
            text, parse_mode=ParseMode.HTML, reply_markup=stats_keyboard(scope)
        )
    except Exception as e:  # noqa: BLE001
        log.debug("stats_scope edit_text: %s", e)
    await call.answer()


# ── new group wizard ─────────────────────────────────────────────────
#
# Реализация на aiogram-dialog: см. [app/bot/dialogs/new_group.py](../dialogs/new_group.py).
# Здесь только entry-point, который стартует dialog.


@router.message(Command("newgroup"))
@router.message(F.text == MENU_NEWGROUP)
async def newgroup_start(message: Message, dialog_manager: DialogManager) -> None:
    tg = message.from_user
    if tg is None:
        return
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, tg.id)
        if teacher is None:
            return
        all_schools = await schools_svc.admin_schools(s, teacher.id)
    from aiogram_dialog import StartMode

    from app.bot.dialogs import NewGroupSG
    if len(all_schools) > 1:
        # Co-admin нескольких школ (или owner + co-admin где-то ещё) — молча
        # выбирать нельзя, иначе группа рискует уйти не в ту школу (см.
        # phantom-school находку аудита). Пусть выберет явно через карточку школы.
        await message.answer(
            "У тебя несколько школ — открой 🏫 Школа → нужную → «➕ Новая группа»."
        )
        return
    data = {"school_id": all_schools[0][0].id} if all_schools else None
    await dialog_manager.start(NewGroupSG.style, mode=StartMode.RESET_STACK, data=data)


async def _format_group_summary(session, group: Group) -> str:
    """Multi-line description used in the group card and after creation."""
    lines = [f"Расписание: {format_weekdays(group.weekdays)} в {group.lesson_time}"]
    if group.style:
        lines.append(f"Направление: {escape(group.style)}")
    if group.level is not None:
        lines.append(f"Уровень: {group.level}")
    if group.venue_id is not None:
        venue = await venues_svc.get_venue(session, group.venue_id)
        if venue is not None:
            place = venue.name
            if venue.address:
                place += f" — {venue.address}"
            if group.room:
                place += f", {group.room}"
            lines.append(f"Где: {escape(place)}")
    elif group.room:
        lines.append(f"Зал: {escape(group.room)}")
    if group.teachers_note:
        lines.append(f"Ведут: {escape(group.teachers_note)}")
    # Co-админы теперь живут на уровне школы — см. карточку школы. Сюда
    # не дублируем, чтобы карточка группы оставалась компактной.
    return "\n".join(lines)


# ── edit existing group ──────────────────────────────────────────────
#
# Реализация на aiogram-dialog: [app/bot/dialogs/edit_group.py](../dialogs/edit_group.py).
# Здесь только entry-point — старт dialog через cb_group_edit.


@router.callback_query(F.data.startswith("grpedit:"))
async def cb_group_edit(call: CallbackQuery, dialog_manager: DialogManager) -> None:
    group_id = int((call.data or "").split(":", 1)[1])
    async with session_scope() as s:
        group = await _owned_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
    from aiogram_dialog import StartMode

    from app.bot.dialogs import EditGroupSG
    await dialog_manager.start(
        EditGroupSG.menu,
        data={"group_id": group_id},
        mode=StartMode.RESET_STACK,
    )
    await call.answer()


# ── groups list & actions ────────────────────────────────────────────


_NO_MY_GROUPS = (
    "У тебя пока нет своих групп. Создай в карточке школы (🏫 Школа → "
    "➕ Новая группа) или попроси owner'а назначить тебя ответственным."
)


async def _send_groups_list(target: Message, tg_id: int) -> None:
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, tg_id)
        groups = await groups_svc.my_groups(s, teacher.id) if teacher else []
    if not groups:
        await target.answer(_NO_MY_GROUPS)
        return
    await target.answer(f"{ui.GROUP} Мои группы:", reply_markup=groups_keyboard(groups))


@router.message(Command("groups"))
@router.message(F.text == MENU_GROUPS)
async def cmd_groups(message: Message) -> None:
    tg = message.from_user
    if not tg:
        return
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, tg.id)
    if teacher is None:
        await show_student_home(message)
        return
    await _send_groups_list(message, tg.id)


@router.callback_query(F.data == "grplist")
async def cb_groups_list(call: CallbackQuery) -> None:
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
        groups = await groups_svc.my_groups(s, teacher.id) if teacher else []
    if not groups:
        await call.message.edit_text(_NO_MY_GROUPS)
    else:
        await call.message.edit_text(
            f"{ui.GROUP} Мои группы:", reply_markup=groups_keyboard(groups)
        )
    await call.answer()


@router.callback_query(F.data.startswith("grp:"))
async def cb_group_card(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    group_id = int(parts[1])
    # origin `s<school_id>` — если пришли из «Группы школы», туда же и вернёмся.
    back_cb, back_label = "grplist", "◀ К списку"
    if len(parts) > 2 and parts[2].startswith("s"):
        back_cb = f"schgroups:{parts[2][1:]}"
        back_label = "◀ К группам школы"
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
        is_owner = (
            teacher is not None
            and await schools_svc.is_owner(s, group.school_id, teacher.id)
        )
        count = await persons_svc.count_members(s, group_id)
        summary = await _format_group_summary(s, group)
        title = format_group_title(group)
        is_active = group.active
        has_upcoming, has_closed = await _group_card_extras(s, group)
        paused_line = (
            "" if is_active
            else f"\n{ui.PAUSE} <b>на паузе</b> — опросы не рассылаются"
        )
        text = (
            f"{ui.title(ui.GROUP, escape(title))}\n"
            f"{summary}{paused_line}\n"
            f"Опрос приходит за {group.lead_hours} ч до занятия\n"
            f"{ui.STUDENTS} {ui.plural_with_count(count, 'ученик', 'ученика', 'учеников')}"
        )
    await call.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=group_actions_keyboard(
            group_id, is_owner, is_active,
            has_upcoming=has_upcoming, has_closed_surveys=has_closed,
            back_cb=back_cb, back_label=back_label,
        ),
    )
    await call.answer()


async def _render_group_admins(call: CallbackQuery, group_id: int) -> bool:
    """Перерисовать экран назначения ответственных. Возвращает False (уже
    ответив на callback) если доступа нет — чтобы вызывающий не отвечал дважды."""
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        if group is None or group.school_id is None:
            await call.answer("Группа не найдена", show_alert=True)
            return False
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if teacher is None or not await schools_svc.is_owner(
            s, group.school_id, teacher.id
        ):
            await call.answer("Только owner назначает ответственных", show_alert=True)
            return False
        members = await schools_svc.members_of(s, group.school_id)
        assigned = await groups_svc.group_admin_ids(s, group.id)
        items = [(a.id, a.full_name, a.id in assigned) for a, _role in members]
        title = format_group_title(group)
    note = (
        "Уведомления группы (опрос-сводка, добор саппортов) идут отмеченным."
        if assigned
        else "Никто не отмечен → уведомления идут всем членам школы (как сейчас)."
    )
    await call.message.edit_text(
        f"👤 Ответственные за «{title}»\n{note}\nТап по имени — переключить.",
        reply_markup=group_admins_keyboard(group_id, items),
    )
    return True


@router.callback_query(F.data.startswith("grpadm:"))
async def cb_group_admins(call: CallbackQuery) -> None:
    group_id = int(call.data.split(":", 1)[1])
    if await _render_group_admins(call, group_id):
        await call.answer()


@router.callback_query(F.data.startswith("grpadmtog:"))
async def cb_group_admin_toggle(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    group_id, admin_id = int(parts[1]), int(parts[2])
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        if group is None or group.school_id is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if teacher is None or not await schools_svc.is_owner(
            s, group.school_id, teacher.id
        ):
            await call.answer()
            return
        if not await schools_svc.is_member(s, group.school_id, admin_id):
            await call.answer()  # назначать можно только членов этой школы
            return
        currently = admin_id in await groups_svc.group_admin_ids(s, group.id)
        await groups_svc.set_group_admin(s, group.id, admin_id, not currently)
    if await _render_group_admins(call, group_id):
        await call.answer()


@router.callback_query(F.data.startswith("grpstu:"))
async def cb_group_students(call: CallbackQuery) -> None:
    group_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
        is_owner = (
            teacher is not None
            and await schools_svc.is_owner(s, group.school_id, teacher.id)
        )
        members = await persons_svc.group_members(s, group_id)
        has_upcoming, has_closed = await _group_card_extras(s, group)
    title = format_group_title(group)
    if members:
        text = f"{ui.STUDENTS} Ученики «{title}» ({len(members)}). Тап по имени — карточка:"
        await call.message.edit_text(text, reply_markup=students_keyboard(group_id, members))
    else:
        text = f"{ui.STUDENTS} В группе «{title}» пока нет учеников. Перешли им ссылку-приглашение."
        await call.message.edit_text(
            text,
            reply_markup=group_actions_keyboard(
                group_id, is_owner,
                has_upcoming=has_upcoming, has_closed_surveys=has_closed,
            ),
        )
    await call.answer()


@router.callback_query(F.data.startswith("stupg:"))
async def cb_students_page(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    group_id, page = int(parts[1]), int(parts[2])
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        members = await persons_svc.group_members(s, group_id)
    if call.message:
        await call.message.edit_reply_markup(
            reply_markup=students_keyboard(group_id, members, page=page)
        )
    await call.answer()


def _person_card_text(person: Person, groups: list[Group], level: int | None) -> str:
    """`level` — уровень person'а в пуле СМОТРЯЩЕГО админа (None = не в его пуле)."""
    if person.gender == Gender.male:
        who = "🕺 парень"
    elif person.gender == Gender.female:
        who = "💃 девушка"
    else:
        who = "— пол не задан"
    if level is None:
        pool = "вне твоего пула"
    else:
        helped = f", помог {person.helped_count}" if person.helped_count else ""
        pool = (
            f"в твоём пуле, ур. {level} (приоритет в picker'е "
            f"автодобора: 3 — первый, 1 — последний){helped}"
        )
    group_lines = "\n".join(f"  • {escape(g.name)}" for g in groups) if groups else "  (нет групп)"
    return (
        f"👤 <b>{escape(person.full_name)}</b>\n"
        f"{who}\n"
        f"саппорт: {pool}\n\n"
        f"группы:\n{group_lines}"
    )


async def _viewer_pool_level(s, tg_id: int, person_id: int) -> tuple[int | None, int | None]:
    """(teacher_admin_id, level в его пуле) для смотрящего админа."""
    teacher = await admins_svc.get_by_tg_id(s, tg_id)
    if teacher is None:
        return None, None
    levels = await persons_svc.admin_pool_levels(s, teacher.id)
    return teacher.id, levels.get(person_id)


@router.callback_query(F.data.startswith("pcard:"))
async def cb_person_card(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    group_id, person_id = int(parts[1]), int(parts[2])
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        person = await s.get(Person, person_id)
        if group is None or person is None or person.school_id != group.school_id:
            await call.answer("Не найдено или нет доступа", show_alert=True)
            return
        groups = await persons_svc.person_groups(s, person.id)
        in_group = group in groups
        _, level = await _viewer_pool_level(s, call.from_user.id, person_id)
        text = _person_card_text(person, groups, level)
    await call.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=person_card_keyboard(
            group_id, person_id, level, person.gender, in_group=in_group,
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pcardlvl:"))
async def cb_person_card_level(call: CallbackQuery) -> None:
    """Cycle уровень person'а в пуле СМОТРЯЩЕГО админа 1→2→3→null. idempotency-
    guard через expected-уровень в callback_data."""
    parts = (call.data or "").split(":")
    group_id, person_id = int(parts[1]), int(parts[2])
    expected = lvl_from_code(parts[3]) if len(parts) > 3 else None
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        person = await s.get(Person, person_id)
        if group is None or person is None or person.school_id != group.school_id:
            await call.answer("Не найдено или нет доступа", show_alert=True)
            return
        teacher_id, level = await _viewer_pool_level(s, call.from_user.id, person_id)
        stale = len(parts) > 3 and level != expected
        if not stale and teacher_id is not None:
            level = None if level == _MAX_LEVEL else (level or 0) + 1
            await persons_svc.set_pool_level(s, teacher_id, person_id, level)
        groups = await persons_svc.person_groups(s, person.id)
        in_group = group in groups
        text = _person_card_text(person, groups, level)
    # Перерисовываем карточку даже при stale — иначе быстрый тап «зависает».
    try:
        await call.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=person_card_keyboard(
                group_id, person_id, level, person.gender, in_group=in_group,
            ),
        )
    except Exception as e:  # noqa: BLE001
        log.debug("pcardlvl edit_text: %s", e)
    await call.answer("Уже обновлено" if stale else "Уровень обновлён")


@router.callback_query(F.data.startswith("pcardgnd:"))
async def cb_person_gender_toggle(call: CallbackQuery) -> None:
    """Переключение пола: male ↔ female; если был None — ставим male.
    idempotency-guard через expected-gender в callback_data."""
    parts = (call.data or "").split(":")
    group_id, person_id = int(parts[1]), int(parts[2])
    expected = gender_from_code(parts[3]) if len(parts) > 3 else None
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        person = await s.get(Person, person_id)
        if group is None or person is None or person.school_id != group.school_id:
            await call.answer("Не найдено или нет доступа", show_alert=True)
            return
        stale = len(parts) > 3 and person.gender != expected
        if not stale:
            new_gender = (
                Gender.female if person.gender == Gender.male else Gender.male
            )
            await persons_svc.set_gender(s, person_id, new_gender)
            await s.refresh(person)
        groups = await persons_svc.person_groups(s, person.id)
        in_group = group in groups
        _, level = await _viewer_pool_level(s, call.from_user.id, person_id)
        text = _person_card_text(person, groups, level)
    try:
        await call.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=person_card_keyboard(
                group_id, person_id, level, person.gender, in_group=in_group,
            ),
        )
    except Exception as e:  # noqa: BLE001
        log.debug("pcardgnd edit_text: %s", e)
    await call.answer("Уже обновлено" if stale else "Пол обновлён")


@router.callback_query(F.data.startswith("pcardadd:"))
async def cb_person_add_to_group(call: CallbackQuery) -> None:
    """Записать гостя постоянно в группу — обратная кнопка к pcardrem."""
    parts = (call.data or "").split(":")
    group_id, person_id = int(parts[1]), int(parts[2])
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        person = await s.get(Person, person_id)
        if group is None or person is None or person.school_id != group.school_id:
            await call.answer("Не найдено или нет доступа", show_alert=True)
            return
        await persons_svc.add_to_group(
            s, group, person.tg_user_id, person.tg_username, person.full_name,
        )
        groups = await persons_svc.person_groups(s, person.id)
        _, level = await _viewer_pool_level(s, call.from_user.id, person_id)
        text = _person_card_text(person, groups, level)
    await call.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=person_card_keyboard(
            group_id, person_id, level, person.gender, in_group=True,
        ),
    )
    await call.answer("Записал постоянно")


@router.callback_query(F.data.startswith("pcardrem:"))
async def cb_person_remove_confirm(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    group_id, person_id = int(parts[1]), int(parts[2])
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        person = await s.get(Person, person_id)
        if group is None or person is None or person.school_id != group.school_id:
            await call.answer("Не найдено или нет доступа", show_alert=True)
            return
        name = person.full_name
        group_name = format_group_title(group)
    await call.message.edit_text(
        f"Убрать {name} из группы «{group_name}»?\n"
        f"Это не удаляет человека — он останется в твоём списке и в пуле, "
        f"просто перестанет получать опросы по этой группе.",
        reply_markup=confirm_remove_from_group_keyboard(group_id, person_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pcardremok:"))
async def cb_person_remove(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    group_id, person_id = int(parts[1]), int(parts[2])
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        person = await s.get(Person, person_id)
        if group is None or person is None or person.school_id != group.school_id:
            await call.answer("Не найдено или нет доступа", show_alert=True)
            return
        removed = await persons_svc.remove_from_group(s, person_id, group_id)
        name = person.full_name
        members = await persons_svc.group_members(s, group_id)
        group_name = format_group_title(group)
    if not removed:
        await call.answer("Уже не в группе", show_alert=True)
        return
    if members:
        text = f"{ui.STUDENTS} Ученики «{group_name}» ({len(members)}). Тап по имени — карточка:"
        await call.message.edit_text(text, reply_markup=students_keyboard(group_id, members))
    else:
        async with session_scope() as s:
            teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
            is_owner = teacher is not None and await _is_owner_of_group_id(s, group_id, teacher.id)
            group = await groups_svc.get_group(s, group_id)
            has_upcoming, has_closed = (
                await _group_card_extras(s, group) if group else (False, False)
            )
        text = (
            f"{ui.STUDENTS} В группе «{group_name}» больше никого нет. "
            "Перешли ссылку-приглашение."
        )
        await call.message.edit_text(
            text,
            reply_markup=group_actions_keyboard(
                group_id, is_owner,
                has_upcoming=has_upcoming, has_closed_surveys=has_closed,
            ),
        )
    await call.answer(f"{name} убран(а)")


async def _send_invite(target: Message, group_name: str, link: str) -> None:
    """Send the invite as a single message: QR photo + caption with link.

    Forward as-is to a chat — students see both QR and link in one go.
    """
    caption = (
        f"📨 Приглашение в «{group_name}»\n\n"
        f"Вставь ссылку в шапку/описание группового чата или закрепи её — "
        f"ученик переходит в один тап и сразу записывается в группу. "
        f"Можно переслать это сообщение ученикам или показать QR офлайн.\n\n"
        f"Ссылка: {link}"
    )
    await target.answer_photo(
        BufferedInputFile(qr_png(link), filename="qr.png"),
        caption=caption,
    )


_PING_PENDING_COOLDOWN = timedelta(seconds=60)
# in-memory (не переживает рестарт — ОК, это просто анти-даблтап на ручную
# рассылку, не источник истины): survey_id → момент последней рассылки.
# Ключ верхнего уровня — сам event loop (не id(loop)), а не голый dict: иначе
# в тестах (каждый со своим loop'ом) состояние утекало бы между прогонами
# и ложно блокировало не связанные друг с другом тесты (тот же паттерн, что
# и call_lock в app/services/supports.py). В проде loop один навсегда.
_ping_cooldowns_by_loop: weakref.WeakKeyDictionary[object, dict[int, datetime]] = (
    weakref.WeakKeyDictionary()
)


def _ping_cooldowns() -> dict[int, datetime]:
    loop = asyncio.get_running_loop()
    cooldowns = _ping_cooldowns_by_loop.get(loop)
    if cooldowns is None:
        cooldowns = {}
        _ping_cooldowns_by_loop[loop] = cooldowns
    return cooldowns


@router.callback_query(F.data.startswith("ping:"))
async def cb_ping_pending(call: CallbackQuery, bot: Bot) -> None:
    """Re-send the attendance survey to everyone who hasn't answered yet."""
    survey_id = int((call.data or "").split(":")[1])
    cooldowns = _ping_cooldowns()
    last_sent = cooldowns.get(survey_id)
    if last_sent is not None and datetime.now(UTC) - last_sent < _PING_PENDING_COOLDOWN:
        await call.answer("Недавно уже напомнил — подожди немного.", show_alert=True)
        return
    async with session_scope() as s:
        survey = await surveys_svc.get_survey(s, survey_id)
        if survey is None:
            await call.answer("Опрос не найден", show_alert=True)
            return
        if survey.status == SurveyStatus.closed:
            await call.answer("Занятие уже прошло — опрос закрыт", show_alert=True)
            return
        group = await groups_svc.get_group(s, survey.group_id)
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
        if group is None or teacher is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        if not await schools_svc.is_member(s, group.school_id, teacher.id):
            await call.answer("Группа не найдена", show_alert=True)
            return
        pending = await surveys_svc.pending_persons(s, survey_id)
        group_name = format_group_title(group)
        lesson_time = group.lesson_time
        lesson_at = survey.lesson_at
        tz_name = (
            await schools_svc.get_timezone(s, group.school_id)
            if group.school_id else None
        )

    if not pending:
        await call.answer("Все уже ответили — пинговать некого", show_alert=True)
        return

    text = (
        f"⏰ <b>{escape(group_name)}</b>\n"
        f"Напоминаю: занятие {format_lesson_at(lesson_at, '%d.%m', tz=tz_name)} "
        f"в {lesson_time}.\n{ui.COMMITMENT}\n\nПридёшь?"
    )
    sent = 0
    for p in pending:
        try:
            await bot.send_message(
                p.tg_user_id,
                text,
                reply_markup=attendance_keyboard(survey_id),
                parse_mode=ParseMode.HTML,
            )
            sent += 1
        except Exception as e:  # noqa: BLE001
            log.warning("Could not ping pending %s: %s", p.tg_user_id, e)
    cooldowns[survey_id] = datetime.now(UTC)
    await call.answer(f"Пингнул {sent}/{len(pending)}", show_alert=True)


@router.callback_query(F.data.startswith("grpinv:"))
async def cb_group_invite(call: CallbackQuery, bot: Bot) -> None:
    group_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        code = group.join_code
        name = format_group_title(group)
    link = await _join_link(bot, code)
    if call.message:
        await _send_invite(call.message, name, link)
    await call.answer()


@router.callback_query(F.data.startswith("grpdel:"))
async def cb_group_delete_confirm(call: CallbackQuery) -> None:
    group_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        group = await _owned_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        name = format_group_title(group)
    await call.message.edit_text(
        f"Удалить группу «{name}»?\n"
        f"Сами ученики останутся в твоём пуле и в других группах — пропадёт только "
        f"эта группа из их расписания.",
        reply_markup=confirm_delete_keyboard(group_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("grpdelok:"))
async def cb_group_delete(call: CallbackQuery) -> None:
    group_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        group = await _owned_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        name = format_group_title(group)
        await groups_svc.delete_group(s, group)
    await call.message.edit_text(f"🗑 Группа «{name}» удалена.")
    await call.answer()


# ── pause / resume group ────────────────────────────────────────────


async def _set_group_active(call: CallbackQuery, group_id: int, active: bool) -> None:
    async with session_scope() as s:
        group = await _owned_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Только owner может ставить группу на паузу", show_alert=True)
            return
        await groups_svc.set_active(s, group, active)
    if active:
        await call.answer("Группа снова в работе")
    else:
        await call.answer("Группа на паузе")
    # Перерисовать карточку группы
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        if group is None:
            return
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
        is_owner = (
            teacher is not None
            and await schools_svc.is_owner(s, group.school_id, teacher.id)
        )
        count = await persons_svc.count_members(s, group_id)
        summary = await _format_group_summary(s, group)
        title = format_group_title(group)
        is_active = group.active
        has_upcoming, has_closed = await _group_card_extras(s, group)
        paused_line = (
            "" if is_active
            else f"\n{ui.PAUSE} <b>на паузе</b> — опросы не рассылаются"
        )
        text = (
            f"{ui.title(ui.GROUP, escape(title))}\n"
            f"{summary}{paused_line}\n"
            f"Опрос приходит за {group.lead_hours} ч до занятия\n"
            f"{ui.STUDENTS} {ui.plural_with_count(count, 'ученик', 'ученика', 'учеников')}"
        )
    if call.message:
        await call.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=group_actions_keyboard(
                group_id, is_owner, is_active,
                has_upcoming=has_upcoming, has_closed_surveys=has_closed,
            ),
        )


@router.callback_query(F.data.startswith("grppause:"))
async def cb_group_pause(call: CallbackQuery) -> None:
    group_id = int(call.data.split(":", 1)[1])
    await _set_group_active(call, group_id, active=False)


@router.callback_query(F.data.startswith("grpresume:"))
async def cb_group_resume(call: CallbackQuery) -> None:
    group_id = int(call.data.split(":", 1)[1])
    await _set_group_active(call, group_id, active=True)


# ── broadcast to group ──────────────────────────────────────────────
# Реализация — [misc_dialogs.broadcast_dialog](../dialogs/misc_dialogs.py).


@router.callback_query(F.data.startswith("grpbcast:"))
async def cb_broadcast_start(call: CallbackQuery, dialog_manager: DialogManager) -> None:
    group_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
    from aiogram_dialog import StartMode

    from app.bot.dialogs import BroadcastSG
    await dialog_manager.start(
        BroadcastSG.text,
        data={"group_id": group_id},
        mode=StartMode.RESET_STACK,
    )
    await call.answer()


# ── post-lesson attendance mark ─────────────────────────────────────
# Реализация — [attendance.py](attendance.py).


# ── cancel single lesson ────────────────────────────────────────────
# Реализация — [lesson_cancel.py](lesson_cancel.py). Здесь только общий no-op
# callback, который ловит "noop" для disabled-кнопок в pick-list.


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()


# ── edit display name ────────────────────────────────────────────────
# Реализация — [misc_dialogs.edit_name_dialog](../dialogs/misc_dialogs.py).


@router.message(Command("name"))
@router.message(F.text == MENU_NAME)
async def cmd_edit_name(message: Message, dialog_manager: DialogManager) -> None:
    tg = message.from_user
    if not tg:
        return
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, tg.id)
        persons_rows = await persons_svc.find_all_by_tg(s, tg.id)
    if teacher is None and not persons_rows:
        await message.answer(
            "Сначала запишись в группу по ссылке от преподавателя — тогда смогу"
            " запомнить, как тебя называть."
        )
        return
    from aiogram_dialog import StartMode

    from app.bot.dialogs import EditNameSG
    await dialog_manager.start(EditNameSG.name, mode=StartMode.RESET_STACK)


