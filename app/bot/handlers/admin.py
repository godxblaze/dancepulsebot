from __future__ import annotations

import logging
import re
from datetime import timedelta
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram_dialog import DialogManager

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
    groups_keyboard,
    lvl_from_code,
    person_card_keyboard,
    students_keyboard,
    today_ping_keyboard,
)
from app.bot.utils import (
    bar,
    format_group_title,
    get_bot_username,
    qr_png,
)
from app.core.config import settings
from app.db.models import Gender, Group, Person, SurveyStatus
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import exports as exports_svc
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import schools as schools_svc
from app.services import surveys as surveys_svc
from app.services import venues as venues_svc
from app.services.schedule import (
    format_weekdays,
    local_now,
    next_lesson_at,
)

log = logging.getLogger(__name__)
router = Router()

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
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


async def _partner_link(bot: Bot, code: str) -> str:
    username = await get_bot_username(bot)
    return f"https://t.me/{username}?start=partner_{code}"


async def _styles_for(session, admin) -> list[str]:
    """Style-подсказки из собственной школы admin'а — используются в мастере
    при создании/редактировании группы."""
    school = await schools_svc.get_owned_school(session, admin.id)
    if school is None:
        return []
    return await groups_svc.school_styles(session, school.id)


async def _venues_for(session, admin) -> list:
    """Venues собственной школы admin'а — для venue-picker'а."""
    school = await schools_svc.get_owned_school(session, admin.id)
    if school is None:
        return []
    return await venues_svc.school_venues(session, school.id)


async def _top_supports_for(session, admin, limit: int = 3) -> list:
    school = await schools_svc.get_owned_school(session, admin.id)
    if school is None:
        return []
    return await persons_svc.top_supports(session, school.id, limit=limit)


async def _long_absent_for(session, admin, since) -> list:
    school = await schools_svc.get_owned_school(session, admin.id)
    if school is None:
        return []
    return await persons_svc.long_absent(session, school.id, since)


async def _students_csv_for(session, admin) -> bytes:
    school = await schools_svc.get_owned_school(session, admin.id)
    if school is None:
        return b""
    return await exports_svc.students_csv(session, school.id)


async def _attendance_csv_for(session, admin) -> bytes:
    school = await schools_svc.get_owned_school(session, admin.id)
    if school is None:
        return b""
    return await exports_svc.attendance_csv(session, school.id)


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
        await call.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    await call.answer()


async def _build_today_view(teacher_id: int, span: str):
    """Возвращает (text, markup) для /today. span ∈ {'day', 'week'}."""
    now = local_now()
    today = now.date()
    horizon_days = 7 if span == "week" else 2  # day → today+tomorrow, week → 7 дней вперёд
    horizon_dates = {(today + timedelta(days=i)) for i in range(horizon_days)}
    blocks: list[str] = []
    pending_by_group: list[tuple[str, int, int]] = []

    async with session_scope() as s:
        groups = await groups_svc.admin_visible_groups(s, teacher_id)
        for g in groups:
            lesson_at = next_lesson_at(g.weekdays, g.lesson_time, now)
            if lesson_at is None or lesson_at.date() not in horizon_dates:
                continue
            if lesson_at.date() == today:
                when = "сегодня"
            elif lesson_at.date() == today + timedelta(days=1):
                when = "завтра"
            else:
                when = f"{lesson_at:%d.%m (%a)}"
            survey = await surveys_svc.find_attendance_survey(s, g.id, lesson_at)
            if survey is None:
                blocks.append(
                    f"👯 <b>{escape(format_group_title(g))}</b> · {when} {g.lesson_time}\n"
                    f"опрос ещё не разослан"
                )
                continue
            summary = await surveys_svc.summarize(s, survey.id)
            pct = round(100 * summary.yes / summary.total) if summary.total else 0
            lines = [
                f"👯 <b>{escape(format_group_title(g))}</b> · {when} {g.lesson_time}",
                f"придут <b>{summary.yes}/{summary.total}</b> ({pct}%)  "
                f"{bar(summary.yes, summary.total)}",
            ]
            if summary.no_names:
                lines.append(f"❌ {escape(', '.join(summary.no_names))}")
            if summary.pending_names:
                lines.append(f"⏳ {escape(', '.join(summary.pending_names))}")
            blocks.append("\n".join(lines))
            if summary.pending > 0:
                pending_by_group.append((g.name, survey.id, summary.pending))

    if not blocks:
        empty = "На неделе занятий нет." if span == "week" else "На сегодня и завтра занятий нет."
        return empty, today_ping_keyboard([], span=span)
    return "\n\n".join(blocks), today_ping_keyboard(pending_by_group, span=span)


@router.message(Command("export"))
@router.message(F.text == MENU_EXPORT)
async def cmd_export(message: Message, bot: Bot) -> None:
    """CSV-выгрузка учеников и посещаемости. Без аргументов — два файла одним
    диалогом. Сами файлы лежат в памяти, ничего не сохраняется на диск."""
    tg = message.from_user
    if not tg:
        return
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, tg.id)
        if teacher is None:
            return  # not an admin — silent
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


@router.message(Command("stats"))
@router.message(F.text == MENU_STATS)
async def cmd_stats(message: Message) -> None:
    tg = message.from_user
    if not tg:
        return
    blocks: list[str] = []

    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, tg.id)
        if teacher is None:
            return
        groups = await groups_svc.admin_visible_groups(s, teacher.id)
        if not groups:
            await message.answer("У тебя пока нет групп. Создай первую: /newgroup")
            return
        top_helpers = await _top_supports_for(s, teacher, limit=3)
        four_weeks_ago = local_now() - timedelta(weeks=4)
        absent = await _long_absent_for(s, teacher, four_weeks_ago)
        for g in groups:
            closed = await surveys_svc.recent_closed_surveys(s, g.id, limit=5)
            if not closed:
                blocks.append(
                    f"📊 <b>{escape(g.name)}</b> — пока нет завершённых опросов"
                )
                continue
            lines = [
                f"📊 <b>{escape(format_group_title(g))}</b> — "
                f"последние {len(closed)} занятий:"
            ]
            total_yes = total_all = 0
            total_actual_attended = total_actual_marked = 0
            for survey in reversed(closed):  # oldest first
                summary = await surveys_svc.summarize(s, survey.id)
                total_yes += summary.yes
                total_all += summary.total
                line = (
                    f"{bar(summary.yes, summary.total)}  "
                    f"{survey.lesson_at:%d.%m} · обещали {summary.yes}/{summary.total}"
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
        top_lines = ["🏆 <b>Топ саппортов</b>"]
        for p in top_helpers:
            top_lines.append(f"• {escape(p.full_name)} — помог {p.helped_count}")
        blocks.append("\n".join(top_lines))

    if absent:
        ab_lines = ["💤 <b>Давно не приходили</b> (≥4 недель или вообще ни разу)"]
        for person, last in absent[:10]:
            tail = f"последний раз {last:%d.%m.%Y}" if last else "ни разу не отмечен"
            ab_lines.append(f"• {escape(person.full_name)} — {tail}")
        if len(absent) > 10:
            ab_lines.append(f"… и ещё {len(absent) - 10}")
        blocks.append("\n".join(ab_lines))

    await message.answer("\n\n".join(blocks), parse_mode=ParseMode.HTML)


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
    from aiogram_dialog import StartMode

    from app.bot.dialogs import NewGroupSG
    await dialog_manager.start(NewGroupSG.style, mode=StartMode.RESET_STACK)


async def _format_group_summary(session, group: Group) -> str:
    """Multi-line description used in the group card and after creation."""
    lines = [f"Расписание: {format_weekdays(group.weekdays)} в {group.lesson_time}"]
    if group.style:
        lines.append(f"Направление: {group.style}")
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
            lines.append(f"Где: {place}")
    elif group.room:
        lines.append(f"Зал: {group.room}")
    if group.teachers_note:
        lines.append(f"Ведут: {group.teachers_note}")
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


async def _send_groups_list(target: Message) -> None:
    tg = target.chat
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, tg.id)
        groups = await groups_svc.admin_visible_groups(s, teacher.id) if teacher else []
    if not groups:
        await target.answer("У тебя пока нет групп. Создай первую: /newgroup")
        return
    await target.answer("Твои группы:", reply_markup=groups_keyboard(groups))


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
    await _send_groups_list(message)


@router.callback_query(F.data == "grplist")
async def cb_groups_list(call: CallbackQuery) -> None:
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
        groups = await groups_svc.admin_visible_groups(s, teacher.id) if teacher else []
    if not groups:
        await call.message.edit_text("У тебя пока нет групп. Создай первую: /newgroup")
    else:
        await call.message.edit_text("Твои группы:", reply_markup=groups_keyboard(groups))
    await call.answer()


@router.callback_query(F.data.startswith("grp:"))
async def cb_group_card(call: CallbackQuery) -> None:
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
        count = await persons_svc.count_members(s, group_id)
        summary = await _format_group_summary(s, group)
        title = format_group_title(group)
        is_active = group.active
        has_upcoming, has_closed = await _group_card_extras(s, group)
        paused_line = "" if is_active else "\n⏸ <b>на паузе</b> — опросы не рассылаются"
        text = (
            f"📋 «{title}»\n"
            f"{summary}{paused_line}\n"
            f"Опрос приходит за {group.lead_hours} ч до занятия\n"
            f"Учеников: {count}"
        )
    await call.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=group_actions_keyboard(
            group_id, is_owner, is_active,
            has_upcoming=has_upcoming, has_closed_surveys=has_closed,
        ),
    )
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
        text = f"👥 Ученики «{title}» ({len(members)}). Тап по имени — карточка:"
        await call.message.edit_text(text, reply_markup=students_keyboard(group_id, members))
    else:
        text = f"👥 В группе «{title}» пока нет учеников. Перешли им ссылку-приглашение."
        await call.message.edit_text(
            text,
            reply_markup=group_actions_keyboard(
                group_id, is_owner,
                has_upcoming=has_upcoming, has_closed_surveys=has_closed,
            ),
        )
    await call.answer()


def _person_card_text(person: Person, groups: list[Group]) -> str:
    if person.gender == Gender.male:
        who = "🕺 парень"
    elif person.gender == Gender.female:
        who = "💃 девушка"
    else:
        who = "— пол не задан"
    if person.support_level is None:
        pool = "вне пула"
    else:
        helped = f", помог {person.helped_count}" if person.helped_count else ""
        pool = (
            f"в пуле, ур. {person.support_level} (приоритет в picker'е "
            f"автодобора: 3 — первый, 1 — последний){helped}"
        )
    group_lines = "\n".join(f"  • {escape(g.name)}" for g in groups) if groups else "  (нет групп)"
    return (
        f"👤 <b>{escape(person.full_name)}</b>\n"
        f"{who}\n"
        f"саппорт: {pool}\n\n"
        f"группы:\n{group_lines}"
    )


@router.callback_query(F.data.startswith("pcard:"))
async def cb_person_card(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    group_id, person_id = int(parts[1]), int(parts[2])
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        person = await s.get(Person, person_id)
        if group is None or person is None or person.school_id != group.school_id:
            await call.answer("Не найдено", show_alert=True)
            return
        groups = await persons_svc.person_groups(s, person.id)
        in_group = group in groups
        text = _person_card_text(person, groups)
    await call.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=person_card_keyboard(
            group_id, person_id, person.support_level, person.gender, in_group=in_group,
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pcardlvl:"))
async def cb_person_card_level(call: CallbackQuery) -> None:
    """Cycle support level 1→2→3→null, then redraw the card. idempotency-guard
    через expected-уровень в callback_data — двойной тап не перескочит."""
    parts = (call.data or "").split(":")
    group_id, person_id = int(parts[1]), int(parts[2])
    expected = lvl_from_code(parts[3]) if len(parts) > 3 else None
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        person = await s.get(Person, person_id)
        if group is None or person is None or person.school_id != group.school_id:
            await call.answer("Не найдено", show_alert=True)
            return
        stale = len(parts) > 3 and person.support_level != expected
        if not stale:
            new_level = (
                None if person.support_level == _MAX_LEVEL
                else (person.support_level or 0) + 1
            )
            await persons_svc.set_support_level(s, person_id, new_level)
            await s.refresh(person)
        groups = await persons_svc.person_groups(s, person.id)
        in_group = group in groups
        text = _person_card_text(person, groups)
    # Перерисовываем карточку даже при stale — иначе быстрый тап «зависает».
    try:
        await call.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=person_card_keyboard(
                group_id, person_id, person.support_level, person.gender,
                in_group=in_group,
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
            await call.answer("Не найдено", show_alert=True)
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
        text = _person_card_text(person, groups)
    try:
        await call.message.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=person_card_keyboard(
                group_id, person_id, person.support_level, person.gender,
                in_group=in_group,
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
            await call.answer("Не найдено", show_alert=True)
            return
        await persons_svc.add_to_group(
            s, group, person.tg_user_id, person.tg_username, person.full_name,
        )
        await s.refresh(person)
        groups = await persons_svc.person_groups(s, person.id)
        text = _person_card_text(person, groups)
    await call.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=person_card_keyboard(
            group_id, person_id, person.support_level, person.gender, in_group=True,
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
            await call.answer("Не найдено", show_alert=True)
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
            await call.answer("Не найдено", show_alert=True)
            return
        removed = await persons_svc.remove_from_group(s, person_id, group_id)
        name = person.full_name
        members = await persons_svc.group_members(s, group_id)
        group_name = format_group_title(group)
    if not removed:
        await call.answer("Уже не в группе", show_alert=True)
        return
    if members:
        text = f"👥 Ученики «{group_name}» ({len(members)}). Тап по имени — карточка:"
        await call.message.edit_text(text, reply_markup=students_keyboard(group_id, members))
    else:
        async with session_scope() as s:
            teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
            is_owner = teacher is not None and await _is_owner_of_group_id(s, group_id, teacher.id)
            group = await groups_svc.get_group(s, group_id)
            has_upcoming, has_closed = (
                await _group_card_extras(s, group) if group else (False, False)
            )
        text = f"👥 В группе «{group_name}» больше никого нет. Перешли ссылку-приглашение."
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
        f"Перешли это сообщение ученикам или покажи QR офлайн.\n\n"
        f"Ссылка: {link}"
    )
    await target.answer_photo(
        BufferedInputFile(qr_png(link), filename="qr.png"),
        caption=caption,
    )


@router.callback_query(F.data.startswith("ping:"))
async def cb_ping_pending(call: CallbackQuery, bot: Bot) -> None:
    """Re-send the attendance survey to everyone who hasn't answered yet."""
    survey_id = int((call.data or "").split(":")[1])
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

    if not pending:
        await call.answer("Все уже ответили — пинговать некого", show_alert=True)
        return

    text = (
        f"⏰ <b>{escape(group_name)}</b>\n"
        f"Напоминаю: занятие {lesson_at:%d.%m} в {lesson_time}.\n\n"
        f"Придёшь?"
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
        paused_line = "" if is_active else "\n⏸ <b>на паузе</b> — опросы не рассылаются"
        text = (
            f"📋 «{title}»\n"
            f"{summary}{paused_line}\n"
            f"Опрос приходит за {group.lead_hours} ч до занятия\n"
            f"Учеников: {count}"
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


