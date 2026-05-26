from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.bot.commands import grant_admin_commands
from app.bot.handlers.student import show_student_home, show_student_today
from app.bot.keyboards import (
    MENU_GROUPS,
    MENU_NAME,
    MENU_NEWGROUP,
    MENU_POOL,
    MENU_STATS,
    MENU_TEXTS,
    MENU_TODAY,
    admin_menu_keyboard,
    attendance_keyboard,
    attendance_mark_keyboard,
    closed_surveys_keyboard,
    coadmin_accept_keyboard,
    coadmins_menu_keyboard,
    confirm_cancel_lesson_keyboard,
    confirm_delete_keyboard,
    confirm_remove_from_group_keyboard,
    group_actions_keyboard,
    group_edit_menu_keyboard,
    groups_keyboard,
    level_picker_keyboard,
    person_card_keyboard,
    pick_lesson_to_cancel_keyboard,
    pool_keyboard,
    skip_keyboard,
    students_keyboard,
    style_picker_keyboard,
    style_typing_keyboard,
    today_ping_keyboard,
    venue_picker_keyboard,
    walk_in_picker_keyboard,
    weekdays_keyboard,
)
from app.bot.states import Broadcast, EditGroup, EditName, NewGroup
from app.bot.utils import (
    bar,
    derive_group_name,
    format_group_title,
    get_bot_username,
    qr_png,
)
from app.core.config import settings
from app.db.models import Answer, Gender, Group, Person, SupportCallStatus, SurveyStatus
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import cancellations as cancellations_svc
from app.services import group_access as access_svc
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import supports as supports_svc
from app.services import surveys as surveys_svc
from app.services import venues as venues_svc
from app.services.schedule import (
    format_weekdays,
    local_now,
    next_lesson_at,
    upcoming_lessons,
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
    "/stats — посещаемость за последние занятия\n"
    "/pool — пул саппортов на подмену\n"
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


async def _coadmin_link(bot: Bot, code: str) -> str:
    username = await get_bot_username(bot)
    return f"https://t.me/{username}?start=coadm_{code}"


# ── registration & help ──────────────────────────────────────────────


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    tg = message.from_user
    if not tg:
        return
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, tg.id)
        if teacher is None and tg.id == settings.developer_id:
            teacher = await admins_svc.get_or_create(
                s, tg.id, tg.username, tg.full_name or f"id{tg.id}"
            )
    if teacher is None:
        # everyone is a student by default — teacher status is granted via /grant
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
            target_id, "✅ Тебе выдан статус преподавателя! Нажми /start."
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Could not notify new teacher %s: %s", target_id, e)


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
    now = local_now()
    today = now.date()
    tomorrow = today + timedelta(days=1)
    blocks: list[str] = []
    pending_by_group: list[tuple[str, int, int]] = []  # name, survey_id, pending

    async with session_scope() as s:
        groups = await groups_svc.admin_groups(s, teacher.id)
        for g in groups:
            lesson_at = next_lesson_at(g.weekdays, g.lesson_time, now)
            if lesson_at is None or lesson_at.date() not in (today, tomorrow):
                continue
            when = "сегодня" if lesson_at.date() == today else "завтра"
            survey = await surveys_svc.find_attendance_survey(s, g.id, lesson_at)
            if survey is None:
                blocks.append(
                    f"👯 <b>{escape(format_group_title(g))}</b> · {when} {g.lesson_time}\n"
                    f"опрос ещё не разослан"
                )
                continue
            summary = await surveys_svc.summarize(s, survey.id)
            lines = [
                f"👯 <b>{escape(format_group_title(g))}</b> · {when} {g.lesson_time}",
                f"придут <b>{summary.yes}/{summary.total}</b>  "
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
        await message.answer("На сегодня и завтра занятий нет.")
        return
    await message.answer(
        "\n\n".join(blocks),
        parse_mode=ParseMode.HTML,
        reply_markup=today_ping_keyboard(pending_by_group),
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
        groups = await groups_svc.admin_groups(s, teacher.id)
        if not groups:
            await message.answer("У тебя пока нет групп. Создай первую: /newgroup")
            return
        top_helpers = await persons_svc.top_supports(s, teacher.id, limit=3)
        four_weeks_ago = local_now() - timedelta(weeks=4)
        absent = await persons_svc.long_absent(s, teacher.id, four_weeks_ago)
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
            for survey in reversed(closed):  # oldest first
                summary = await surveys_svc.summarize(s, survey.id)
                total_yes += summary.yes
                total_all += summary.total
                line = (
                    f"{bar(summary.yes, summary.total)}  "
                    f"{survey.lesson_at:%d.%m} · {summary.yes}/{summary.total}"
                )
                if summary.actual_attended is not None:
                    line += f" · реально пришло {summary.actual_attended}"
                lines.append(line)
            if total_all:
                pct = round(100 * total_yes / total_all)
                lines.append(f"средний отклик: <b>{pct}%</b>")
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
# Order: style → level → weekdays → lesson_time → venue → room → teachers_note.
# No separate display-name step — Group.name is auto-derived from style + level.


@router.message(Command("newgroup"))
@router.message(F.text == MENU_NEWGROUP)
async def newgroup_start(message: Message, state: FSMContext) -> None:
    tg = message.from_user
    assert tg is not None
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, tg.id)
        suggestions = (
            await groups_svc.admin_styles(s, teacher.id) if teacher else []
        )
    if teacher is None:
        return
    await state.set_state(NewGroup.style)
    await state.update_data(weekdays=[])
    await message.answer(
        "Какое направление? (например: Бачата, Сальса, Хип-хоп)",
        reply_markup=style_picker_keyboard(suggestions),
    )


@router.callback_query(NewGroup.style, F.data.startswith("ngst:"))
async def newgroup_style_pick(call: CallbackQuery, state: FSMContext) -> None:
    action = (call.data or "").split(":", 1)[1]
    if action == "skip":
        await state.update_data(style=None)
        await _advance_to_level(call.message, state)
        await call.answer()
        return
    if action == "type":
        await state.set_state(NewGroup.style_typing)
        await call.message.edit_text(
            "Напиши направление одной строкой:",
            reply_markup=style_typing_keyboard(),
        )
        await call.answer()
        return
    if action.startswith("pick:"):
        idx = int(action.split(":", 1)[1])
        tg = call.from_user
        async with session_scope() as s:
            teacher = await admins_svc.get_by_tg_id(s, tg.id)
            suggestions = (
                await groups_svc.admin_styles(s, teacher.id) if teacher else []
            )
        if not (0 <= idx < len(suggestions)):
            await call.answer()
            return
        await state.update_data(style=suggestions[idx])
        await _advance_to_level(call.message, state)
        await call.answer()


@router.callback_query(NewGroup.style_typing, F.data == "ngst:skip")
async def newgroup_style_typing_skip(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(style=None)
    await _advance_to_level(call.message, state)
    await call.answer()


@router.message(NewGroup.style_typing, F.text, ~F.text.in_(MENU_TEXTS))
async def newgroup_style_typed(message: Message, state: FSMContext) -> None:
    style = (message.text or "").strip()
    if not style:
        await message.answer(
            "Пустое направление — напиши ещё раз или нажми «Пропустить»."
        )
        return
    await state.update_data(style=style)
    await _advance_to_level(message, state)


async def _advance_to_level(target: Message, state: FSMContext) -> None:
    await state.set_state(NewGroup.level)
    await target.answer(
        "Какой уровень? (0–4 от новичков до продвинутых)",
        reply_markup=level_picker_keyboard("nglv"),
    )


@router.callback_query(NewGroup.level, F.data.startswith("nglv:"))
async def newgroup_level_pick(call: CallbackQuery, state: FSMContext) -> None:
    action = (call.data or "").split(":", 1)[1]
    if action == "skip":
        await state.update_data(level=None)
        await _advance_to_weekdays(call.message, state)
        await call.answer()
        return
    if action.startswith("pick:"):
        level = int(action.split(":", 1)[1])
        if not 0 <= level <= 4:
            await call.answer()
            return
        await state.update_data(level=level)
        await _advance_to_weekdays(call.message, state)
        await call.answer()


async def _advance_to_weekdays(target: Message, state: FSMContext) -> None:
    await state.set_state(NewGroup.weekdays)
    await target.answer(
        "В какие дни занятия? Отметь дни и нажми «Готово».",
        reply_markup=weekdays_keyboard(set()),
    )


@router.callback_query(NewGroup.weekdays, F.data.startswith("wd:"))
async def newgroup_weekdays(call: CallbackQuery, state: FSMContext) -> None:
    action = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected: list[int] = list(data.get("weekdays", []))

    if action == "done":
        if not selected:
            await call.answer("Выбери хотя бы один день", show_alert=True)
            return
        await state.update_data(weekdays=sorted(selected))
        await state.set_state(NewGroup.lesson_time)
        await call.message.edit_text(
            "Во сколько начинается занятие? Формат ЧЧ:ММ, например 19:00"
        )
        await call.answer()
        return

    idx = int(action)
    if idx in selected:
        selected.remove(idx)
    else:
        selected.append(idx)
    await state.update_data(weekdays=selected)
    await call.message.edit_reply_markup(reply_markup=weekdays_keyboard(set(selected)))
    await call.answer()


@router.message(NewGroup.lesson_time, F.text, ~F.text.in_(MENU_TEXTS))
async def newgroup_time(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = (message.text or "").strip()
    m = _TIME_RE.match(raw)
    if not m:
        await message.answer("Не похоже на время. Нужен формат ЧЧ:ММ, например 19:00")
        return
    lesson_time = f"{int(m.group(1)):02d}:{m.group(2)}"
    await state.update_data(lesson_time=lesson_time)

    tg = message.from_user
    assert tg is not None
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, tg.id)
        if teacher is None:
            await state.clear()
            return
        venues = await venues_svc.admin_venues(s, teacher.id)

    await state.set_state(NewGroup.venue)
    await message.answer(
        "Где проходят занятия?",
        reply_markup=venue_picker_keyboard(venues),
    )


@router.callback_query(NewGroup.venue, F.data.startswith("ngvn:"))
async def newgroup_venue_pick(call: CallbackQuery, state: FSMContext) -> None:
    action = (call.data or "").split(":", 1)[1]
    if action == "skip":
        await state.update_data(venue_id=None)
        await _advance_to_room(call.message, state)
        await call.answer()
        return
    if action == "new":
        await state.set_state(NewGroup.venue_new_name)
        await call.message.edit_text(
            "Название площадки одной строкой "
            "(например: «Шлюзовая 8с2» или «Студия Полевик»):"
        )
        await call.answer()
        return
    if action.startswith("pick:"):
        venue_id = int(action.split(":", 1)[1])
        tg = call.from_user
        async with session_scope() as s:
            teacher = await admins_svc.get_by_tg_id(s, tg.id)
            venue = await venues_svc.get_venue(s, venue_id)
            if teacher is None or venue is None or venue.admin_id != teacher.id:
                await call.answer()
                return
        await state.update_data(venue_id=venue_id)
        await _advance_to_room(call.message, state)
        await call.answer()


@router.message(NewGroup.venue_new_name, F.text, ~F.text.in_(MENU_TEXTS))
async def newgroup_venue_new_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Пустое название — введи ещё раз.")
        return
    await state.update_data(venue_new_name=name)
    await state.set_state(NewGroup.venue_new_address)
    await message.answer(
        "Адрес (если хочешь). Можно пропустить:",
        reply_markup=skip_keyboard("ngvn:addrskip"),
    )


@router.callback_query(NewGroup.venue_new_address, F.data == "ngvn:addrskip")
async def newgroup_venue_new_address_skip(
    call: CallbackQuery, state: FSMContext
) -> None:
    await _save_new_venue(call.from_user.id, state, address=None)
    await _advance_to_room(call.message, state)
    await call.answer()


@router.message(NewGroup.venue_new_address, F.text, ~F.text.in_(MENU_TEXTS))
async def newgroup_venue_new_address_typed(
    message: Message, state: FSMContext
) -> None:
    address = (message.text or "").strip() or None
    await _save_new_venue(message.from_user.id, state, address=address)
    await _advance_to_room(message, state)


async def _save_new_venue(
    tg_user_id: int, state: FSMContext, address: str | None
) -> None:
    data = await state.get_data()
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, tg_user_id)
        if teacher is None:
            return
        venue = await venues_svc.create_venue(
            s, teacher.id, data["venue_new_name"], address
        )
        await state.update_data(venue_id=venue.id)


async def _advance_to_room(target: Message, state: FSMContext) -> None:
    await state.set_state(NewGroup.room)
    await target.answer(
        "В каком зале проходит занятие? (например: «зал 38»). Можно пропустить:",
        reply_markup=skip_keyboard("ngrm:skip"),
    )


@router.callback_query(NewGroup.room, F.data == "ngrm:skip")
async def newgroup_room_skip(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(room=None)
    await _advance_to_teachers(call.message, state)
    await call.answer()


@router.message(NewGroup.room, F.text, ~F.text.in_(MENU_TEXTS))
async def newgroup_room_typed(message: Message, state: FSMContext) -> None:
    room = (message.text or "").strip() or None
    await state.update_data(room=room)
    await _advance_to_teachers(message, state)


async def _advance_to_teachers(target: Message, state: FSMContext) -> None:
    await state.set_state(NewGroup.teachers_note)
    await target.answer(
        "Кто ведёт? Свободной строкой — например, «Маша + Петя». Можно пропустить:",
        reply_markup=skip_keyboard("ngtn:skip"),
    )


@router.callback_query(NewGroup.teachers_note, F.data == "ngtn:skip")
async def newgroup_teachers_skip(
    call: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    await _finish_new_group(
        call.message,
        state,
        bot,
        tg_user_id=call.from_user.id,
        teachers_note=None,
    )
    await call.answer()


@router.message(NewGroup.teachers_note, F.text, ~F.text.in_(MENU_TEXTS))
async def newgroup_teachers_typed(
    message: Message, state: FSMContext, bot: Bot
) -> None:
    note = (message.text or "").strip() or None
    await _finish_new_group(
        message, state, bot, tg_user_id=message.from_user.id, teachers_note=note
    )


async def _finish_new_group(
    target: Message,
    state: FSMContext,
    bot: Bot,
    tg_user_id: int,
    teachers_note: str | None,
) -> None:
    data = await state.get_data()
    weekdays = ",".join(str(d) for d in data["weekdays"])
    style = data.get("style")
    level = data.get("level")
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, tg_user_id)
        if teacher is None:
            await state.clear()
            return
        group = await groups_svc.create_group(
            s,
            teacher.id,
            derive_group_name(style, level),
            weekdays,
            data["lesson_time"],
            settings.default_lead_hours,
            style=style,
            level=level,
            venue_id=data.get("venue_id"),
            room=data.get("room"),
            teachers_note=teachers_note,
        )
        code = group.join_code
        title = format_group_title(group)
        summary = await _format_group_summary(s, group)

    await state.clear()
    link = await _join_link(bot, code)
    await target.answer(f"✅ Группа «{title}» создана.\n{summary}")
    await _send_invite(target, title, link)


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
    coadmins = await access_svc.coadmins_of_group(session, group.id)
    if coadmins:
        lines.append("🛠 Co-админы: " + ", ".join(a.full_name for a in coadmins))
    return "\n".join(lines)


# ── edit existing group ──────────────────────────────────────────────


async def _show_edit_menu(call: CallbackQuery, group_id: int) -> None:
    """Render the edit-menu in place of whichever message triggered it."""
    async with session_scope() as s:
        group = await _owned_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        title = format_group_title(group)
        summary = await _format_group_summary(s, group)
    if call.message:
        await call.message.edit_text(
            f"✏️ «{title}»\n{summary}\n\nЧто меняем?",
            reply_markup=group_edit_menu_keyboard(group_id),
        )
    await call.answer()


@router.callback_query(F.data.startswith("grpedit:"))
async def cb_group_edit(call: CallbackQuery) -> None:
    group_id = int(call.data.split(":", 1)[1])
    await _show_edit_menu(call, group_id)


async def _return_to_edit_menu(message: Message, group_id: int) -> None:
    """After saving a single field, re-show the edit menu via a fresh message."""
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        if group is None:
            return
        title = format_group_title(group)
        summary = await _format_group_summary(s, group)
    await message.answer(
        f"✅ Сохранил.\n\n✏️ «{title}»\n{summary}\n\nЧто ещё меняем?",
        reply_markup=group_edit_menu_keyboard(group_id),
    )


async def _edit_menu_in_place(target: Message, group_id: int) -> None:
    """Re-render the edit menu over the same message used for the prompt."""
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        if group is None:
            return
        title = format_group_title(group)
        summary = await _format_group_summary(s, group)
    await target.edit_text(
        f"✅ Сохранил.\n\n✏️ «{title}»\n{summary}\n\nЧто ещё меняем?",
        reply_markup=group_edit_menu_keyboard(group_id),
    )


# Each ge:<field>:<gid> opens the matching mini-flow.


@router.callback_query(F.data.startswith("ge:style:"))
async def cb_edit_style(call: CallbackQuery, state: FSMContext) -> None:
    group_id = int(call.data.split(":")[2])
    async with session_scope() as s:
        group = await _owned_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
        suggestions = await groups_svc.admin_styles(s, teacher.id) if teacher else []
    await state.set_state(EditGroup.style)
    await state.update_data(edit_gid=group_id)
    if call.message:
        await call.message.edit_text(
            "Новое направление?",
            reply_markup=style_picker_keyboard(suggestions),
        )
    await call.answer()


@router.callback_query(EditGroup.style, F.data.startswith("ngst:"))
async def edit_style_pick(call: CallbackQuery, state: FSMContext) -> None:
    action = (call.data or "").split(":", 1)[1]
    data = await state.get_data()
    gid = int(data["edit_gid"])
    if action == "skip":
        await _apply_group_edit(gid, style=None)
        await state.clear()
        await _edit_menu_in_place(call.message, gid)
        await call.answer()
        return
    if action == "type":
        await state.set_state(EditGroup.style_typing)
        await call.message.edit_text(
            "Напиши новое направление одной строкой:",
            reply_markup=style_typing_keyboard(),
        )
        await call.answer()
        return
    if action.startswith("pick:"):
        idx = int(action.split(":", 1)[1])
        async with session_scope() as s:
            teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
            suggestions = (
                await groups_svc.admin_styles(s, teacher.id) if teacher else []
            )
        if not (0 <= idx < len(suggestions)):
            await call.answer()
            return
        await _apply_group_edit(gid, style=suggestions[idx])
        await state.clear()
        await _edit_menu_in_place(call.message, gid)
        await call.answer()


@router.callback_query(EditGroup.style_typing, F.data == "ngst:skip")
async def edit_style_typing_skip(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    gid = int(data["edit_gid"])
    await _apply_group_edit(gid, style=None)
    await state.clear()
    await _edit_menu_in_place(call.message, gid)
    await call.answer()


@router.message(EditGroup.style_typing, F.text, ~F.text.in_(MENU_TEXTS))
async def edit_style_typed(message: Message, state: FSMContext) -> None:
    style = (message.text or "").strip()
    if not style:
        await message.answer("Пустое направление — напиши ещё раз.")
        return
    data = await state.get_data()
    gid = int(data["edit_gid"])
    await _apply_group_edit(gid, style=style)
    await state.clear()
    await _return_to_edit_menu(message, gid)


@router.callback_query(F.data.startswith("ge:level:"))
async def cb_edit_level(call: CallbackQuery, state: FSMContext) -> None:
    group_id = int(call.data.split(":")[2])
    async with session_scope() as s:
        if await _owned_group(s, group_id, call.from_user.id) is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
    await state.set_state(EditGroup.level)
    await state.update_data(edit_gid=group_id)
    if call.message:
        await call.message.edit_text(
            "Новый уровень?",
            reply_markup=level_picker_keyboard("nglv"),
        )
    await call.answer()


@router.callback_query(EditGroup.level, F.data.startswith("nglv:"))
async def edit_level_pick(call: CallbackQuery, state: FSMContext) -> None:
    action = (call.data or "").split(":", 1)[1]
    data = await state.get_data()
    gid = int(data["edit_gid"])
    if action == "skip":
        await _apply_group_edit(gid, level=None)
    elif action.startswith("pick:"):
        level = int(action.split(":", 1)[1])
        if not 0 <= level <= 4:
            await call.answer()
            return
        await _apply_group_edit(gid, level=level)
    await state.clear()
    await _edit_menu_in_place(call.message, gid)
    await call.answer()


@router.callback_query(F.data.startswith("ge:days:"))
async def cb_edit_days(call: CallbackQuery, state: FSMContext) -> None:
    group_id = int(call.data.split(":")[2])
    async with session_scope() as s:
        group = await _owned_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        existing = [int(d) for d in (group.weekdays.split(",") if group.weekdays else []) if d]
    await state.set_state(EditGroup.weekdays)
    await state.update_data(edit_gid=group_id, weekdays=existing)
    if call.message:
        await call.message.edit_text(
            "Новые дни? Отметь и нажми «Готово».",
            reply_markup=weekdays_keyboard(set(existing)),
        )
    await call.answer()


@router.callback_query(EditGroup.weekdays, F.data.startswith("wd:"))
async def edit_weekdays(call: CallbackQuery, state: FSMContext) -> None:
    action = call.data.split(":", 1)[1]
    data = await state.get_data()
    gid = int(data["edit_gid"])
    selected: list[int] = list(data.get("weekdays", []))

    if action == "done":
        if not selected:
            await call.answer("Выбери хотя бы один день", show_alert=True)
            return
        await _apply_group_edit(gid, weekdays=",".join(str(d) for d in sorted(selected)))
        await state.clear()
        await _edit_menu_in_place(call.message, gid)
        await call.answer()
        return

    idx = int(action)
    if idx in selected:
        selected.remove(idx)
    else:
        selected.append(idx)
    await state.update_data(weekdays=selected)
    await call.message.edit_reply_markup(reply_markup=weekdays_keyboard(set(selected)))
    await call.answer()


@router.callback_query(F.data.startswith("ge:time:"))
async def cb_edit_time(call: CallbackQuery, state: FSMContext) -> None:
    group_id = int(call.data.split(":")[2])
    async with session_scope() as s:
        if await _owned_group(s, group_id, call.from_user.id) is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
    await state.set_state(EditGroup.lesson_time)
    await state.update_data(edit_gid=group_id)
    if call.message:
        await call.message.edit_text(
            "Новое время? Формат ЧЧ:ММ, например 19:00"
        )
    await call.answer()


@router.message(EditGroup.lesson_time, F.text, ~F.text.in_(MENU_TEXTS))
async def edit_time_typed(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    m = _TIME_RE.match(raw)
    if not m:
        await message.answer("Не похоже на время. Нужен формат ЧЧ:ММ")
        return
    lesson_time = f"{int(m.group(1)):02d}:{m.group(2)}"
    data = await state.get_data()
    gid = int(data["edit_gid"])
    await _apply_group_edit(gid, lesson_time=lesson_time)
    await state.clear()
    await _return_to_edit_menu(message, gid)


@router.callback_query(F.data.startswith("ge:venue:"))
async def cb_edit_venue(call: CallbackQuery, state: FSMContext) -> None:
    group_id = int(call.data.split(":")[2])
    async with session_scope() as s:
        group = await _owned_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
        venues = await venues_svc.admin_venues(s, teacher.id) if teacher else []
    await state.set_state(EditGroup.venue)
    await state.update_data(edit_gid=group_id)
    if call.message:
        await call.message.edit_text(
            "Новое место?",
            reply_markup=venue_picker_keyboard(venues),
        )
    await call.answer()


@router.callback_query(EditGroup.venue, F.data.startswith("ngvn:"))
async def edit_venue_pick(call: CallbackQuery, state: FSMContext) -> None:
    action = (call.data or "").split(":", 1)[1]
    data = await state.get_data()
    gid = int(data["edit_gid"])
    if action == "skip":
        await _apply_group_edit(gid, venue_id=None)
        await state.clear()
        await _edit_menu_in_place(call.message, gid)
        await call.answer()
        return
    if action == "new":
        await state.set_state(EditGroup.venue_new_name)
        await call.message.edit_text(
            "Название площадки одной строкой:"
        )
        await call.answer()
        return
    if action.startswith("pick:"):
        venue_id = int(action.split(":", 1)[1])
        async with session_scope() as s:
            teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
            venue = await venues_svc.get_venue(s, venue_id)
            if teacher is None or venue is None or venue.admin_id != teacher.id:
                await call.answer()
                return
        await _apply_group_edit(gid, venue_id=venue_id)
        await state.clear()
        await _edit_menu_in_place(call.message, gid)
        await call.answer()


@router.message(EditGroup.venue_new_name, F.text, ~F.text.in_(MENU_TEXTS))
async def edit_venue_new_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Пустое название — введи ещё раз.")
        return
    await state.update_data(venue_new_name=name)
    await state.set_state(EditGroup.venue_new_address)
    await message.answer(
        "Адрес (если хочешь). Можно пропустить:",
        reply_markup=skip_keyboard("ngvn:addrskip"),
    )


@router.callback_query(EditGroup.venue_new_address, F.data == "ngvn:addrskip")
async def edit_venue_new_address_skip(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    gid = int(data["edit_gid"])
    venue_id = await _save_new_venue_for_edit(call.from_user.id, state, address=None)
    if venue_id is not None:
        await _apply_group_edit(gid, venue_id=venue_id)
    await state.clear()
    await _edit_menu_in_place(call.message, gid)
    await call.answer()


@router.message(EditGroup.venue_new_address, F.text, ~F.text.in_(MENU_TEXTS))
async def edit_venue_new_address_typed(message: Message, state: FSMContext) -> None:
    address = (message.text or "").strip() or None
    data = await state.get_data()
    gid = int(data["edit_gid"])
    venue_id = await _save_new_venue_for_edit(message.from_user.id, state, address=address)
    if venue_id is not None:
        await _apply_group_edit(gid, venue_id=venue_id)
    await state.clear()
    await _return_to_edit_menu(message, gid)


async def _save_new_venue_for_edit(
    tg_user_id: int, state: FSMContext, address: str | None
) -> int | None:
    data = await state.get_data()
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, tg_user_id)
        if teacher is None:
            return None
        venue = await venues_svc.create_venue(
            s, teacher.id, data["venue_new_name"], address
        )
        return venue.id


@router.callback_query(F.data.startswith("ge:room:"))
async def cb_edit_room(call: CallbackQuery, state: FSMContext) -> None:
    group_id = int(call.data.split(":")[2])
    async with session_scope() as s:
        if await _owned_group(s, group_id, call.from_user.id) is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
    await state.set_state(EditGroup.room)
    await state.update_data(edit_gid=group_id)
    if call.message:
        await call.message.edit_text(
            "Новый зал? Или нажми «Пропустить», чтобы очистить.",
            reply_markup=skip_keyboard("ngrm:skip"),
        )
    await call.answer()


@router.callback_query(EditGroup.room, F.data == "ngrm:skip")
async def edit_room_skip(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    gid = int(data["edit_gid"])
    await _apply_group_edit(gid, room=None)
    await state.clear()
    await _edit_menu_in_place(call.message, gid)
    await call.answer()


@router.message(EditGroup.room, F.text, ~F.text.in_(MENU_TEXTS))
async def edit_room_typed(message: Message, state: FSMContext) -> None:
    room = (message.text or "").strip() or None
    data = await state.get_data()
    gid = int(data["edit_gid"])
    await _apply_group_edit(gid, room=room)
    await state.clear()
    await _return_to_edit_menu(message, gid)


@router.callback_query(F.data.startswith("ge:teachers:"))
async def cb_edit_teachers(call: CallbackQuery, state: FSMContext) -> None:
    group_id = int(call.data.split(":")[2])
    async with session_scope() as s:
        if await _owned_group(s, group_id, call.from_user.id) is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
    await state.set_state(EditGroup.teachers_note)
    await state.update_data(edit_gid=group_id)
    if call.message:
        await call.message.edit_text(
            "Кто ведёт? Свободной строкой. Можно пропустить, чтобы очистить.",
            reply_markup=skip_keyboard("ngtn:skip"),
        )
    await call.answer()


@router.callback_query(EditGroup.teachers_note, F.data == "ngtn:skip")
async def edit_teachers_skip(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    gid = int(data["edit_gid"])
    await _apply_group_edit(gid, teachers_note=None)
    await state.clear()
    await _edit_menu_in_place(call.message, gid)
    await call.answer()


@router.message(EditGroup.teachers_note, F.text, ~F.text.in_(MENU_TEXTS))
async def edit_teachers_typed(message: Message, state: FSMContext) -> None:
    note = (message.text or "").strip() or None
    data = await state.get_data()
    gid = int(data["edit_gid"])
    await _apply_group_edit(gid, teachers_note=note)
    await state.clear()
    await _return_to_edit_menu(message, gid)


async def _apply_group_edit(group_id: int, **fields) -> None:
    """Persist a single-field edit. Re-derives Group.name whenever style/level changes."""
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        if group is None:
            return
        await groups_svc.update_group(s, group, **fields)
        if "style" in fields or "level" in fields:
            await groups_svc.update_group(
                s, group, name=derive_group_name(group.style, group.level)
            )


# ── groups list & actions ────────────────────────────────────────────


async def _accessible_group(session, group_id: int, tg_user_id: int) -> Group | None:
    """Return the group if the caller is owner OR co-admin; None otherwise.

    Use this for «view» actions: opening card, students roster, ping, /today.
    For destructive actions (delete, edit settings) use _owned_group instead."""
    teacher = await admins_svc.get_by_tg_id(session, tg_user_id)
    if teacher is None:
        return None
    group = await groups_svc.get_group(session, group_id)
    if group is None:
        return None
    if not await access_svc.has_access(session, group.id, teacher.id):
        return None
    return group


async def _owned_group(session, group_id: int, tg_user_id: int) -> Group | None:
    """Like _accessible_group but only the owner — for destructive actions."""
    teacher = await admins_svc.get_by_tg_id(session, tg_user_id)
    if teacher is None:
        return None
    group = await groups_svc.get_group(session, group_id)
    if group is None:
        return None
    if not await access_svc.is_owner(session, group.id, teacher.id):
        return None
    return group


async def _send_groups_list(target: Message) -> None:
    tg = target.chat
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, tg.id)
        groups = await groups_svc.admin_groups(s, teacher.id) if teacher else []
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
        groups = await groups_svc.admin_groups(s, teacher.id) if teacher else []
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
        is_owner = teacher is not None and await access_svc.is_owner(
            s, group.id, teacher.id
        )
        count = await persons_svc.count_members(s, group_id)
        summary = await _format_group_summary(s, group)
        title = format_group_title(group)
        is_active = group.active
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
        reply_markup=group_actions_keyboard(group_id, is_owner, is_active),
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
        is_owner = teacher is not None and await access_svc.is_owner(
            s, group.id, teacher.id
        )
        members = await persons_svc.group_members(s, group_id)
    title = format_group_title(group)
    if members:
        text = f"👥 Ученики «{title}» ({len(members)}). Тап по имени — карточка:"
        await call.message.edit_text(text, reply_markup=students_keyboard(group_id, members))
    else:
        text = f"👥 В группе «{title}» пока нет учеников. Перешли им ссылку-приглашение."
        await call.message.edit_text(text, reply_markup=group_actions_keyboard(group_id, is_owner))
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
        pool = f"в пуле, ур. {person.support_level}{helped}"
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
        if group is None or person is None or person.admin_id != group.admin_id:
            await call.answer("Не найдено", show_alert=True)
            return
        groups = await persons_svc.person_groups(s, person.id)
        text = _person_card_text(person, groups)
    await call.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=person_card_keyboard(group_id, person_id, person.support_level, person.gender),
    )
    await call.answer()


@router.callback_query(F.data.startswith("pcardlvl:"))
async def cb_person_card_level(call: CallbackQuery) -> None:
    """Cycle support level 1→2→3→null, then redraw the card."""
    parts = (call.data or "").split(":")
    group_id, person_id = int(parts[1]), int(parts[2])
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        person = await s.get(Person, person_id)
        if group is None or person is None or person.admin_id != group.admin_id:
            await call.answer("Не найдено", show_alert=True)
            return
        new_level = None if person.support_level == _MAX_LEVEL else (person.support_level or 0) + 1
        await persons_svc.set_support_level(s, person_id, new_level)
        await s.refresh(person)
        groups = await persons_svc.person_groups(s, person.id)
        text = _person_card_text(person, groups)
    await call.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=person_card_keyboard(group_id, person_id, person.support_level, person.gender),
    )
    await call.answer("Уровень обновлён")


@router.callback_query(F.data.startswith("pcardgnd:"))
async def cb_person_gender_toggle(call: CallbackQuery) -> None:
    """Переключение пола: male ↔ female; если был None — ставим male."""
    parts = (call.data or "").split(":")
    group_id, person_id = int(parts[1]), int(parts[2])
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        person = await s.get(Person, person_id)
        if group is None or person is None or person.admin_id != group.admin_id:
            await call.answer("Не найдено", show_alert=True)
            return
        new_gender = Gender.female if person.gender == Gender.male else Gender.male
        await persons_svc.set_gender(s, person_id, new_gender)
        await s.refresh(person)
        groups = await persons_svc.person_groups(s, person.id)
        text = _person_card_text(person, groups)
    await call.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=person_card_keyboard(group_id, person_id, person.support_level, person.gender),
    )
    await call.answer("Пол обновлён")


@router.callback_query(F.data.startswith("pcardrem:"))
async def cb_person_remove_confirm(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    group_id, person_id = int(parts[1]), int(parts[2])
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        person = await s.get(Person, person_id)
        if group is None or person is None or person.admin_id != group.admin_id:
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
        if group is None or person is None or person.admin_id != group.admin_id:
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
            is_owner = teacher is not None and await access_svc.is_owner(
                s, group_id, teacher.id
            )
        text = f"👥 В группе «{group_name}» больше никого нет. Перешли ссылку-приглашение."
        await call.message.edit_text(text, reply_markup=group_actions_keyboard(group_id, is_owner))
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
        if not await access_svc.has_access(s, group.id, teacher.id):
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
        is_owner = teacher is not None and await access_svc.is_owner(
            s, group.id, teacher.id
        )
        count = await persons_svc.count_members(s, group_id)
        summary = await _format_group_summary(s, group)
        title = format_group_title(group)
        is_active = group.active
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
            reply_markup=group_actions_keyboard(group_id, is_owner, is_active),
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


@router.callback_query(F.data.startswith("grpbcast:"))
async def cb_broadcast_start(call: CallbackQuery, state: FSMContext) -> None:
    group_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        count = await persons_svc.count_members(s, group_id)
        title = format_group_title(group)
    await state.set_state(Broadcast.text)
    await state.update_data(group_id=group_id)
    await call.message.answer(
        f"📢 Напиши сообщение, которое отправлю всем ученикам «{title}» ({count} чел.). "
        f"Чтобы передумать — пришли /cancel."
    )
    await call.answer()


@router.message(Command("cancel"), Broadcast.text)
async def broadcast_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменил, ничего не рассылаю.")


@router.message(Broadcast.text, F.text, ~F.text.in_(MENU_TEXTS))
async def broadcast_send(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    group_id = data.get("group_id")
    await state.clear()
    if group_id is None:
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пустое сообщение — отменяю.")
        return
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, message.from_user.id)
        if group is None:
            await message.answer("Группа не найдена.")
            return
        members = await persons_svc.group_members(s, group_id)
        title = format_group_title(group)
        sender_name = message.from_user.full_name or message.from_user.username or "препод"
    if not members:
        await message.answer("В группе нет учеников — рассылать некому.")
        return
    body = f"📢 «{escape(title)}» — сообщение от {escape(sender_name)}:\n\n{escape(text)}"
    sent = blocked = failed = 0
    from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound
    for p in members:
        try:
            await bot.send_message(p.tg_user_id, body, parse_mode=ParseMode.HTML)
            sent += 1
        except (TelegramForbiddenError, TelegramNotFound):
            blocked += 1
        except Exception as e:  # noqa: BLE001
            log.warning("broadcast to %s failed: %s", p.tg_user_id, e)
            failed += 1
    parts = [f"📢 Отправлено: {sent}/{len(members)}"]
    if blocked:
        parts.append(f"заблокировали бота: {blocked}")
    if failed:
        parts.append(f"не доехало: {failed}")
    await message.answer(" · ".join(parts))


# ── post-lesson attendance mark ─────────────────────────────────────


@router.callback_query(F.data.startswith("grpattnmark:"))
async def cb_attendance_pick_lesson(call: CallbackQuery) -> None:
    """Показываем 5 последних закрытых занятий — preпод выбирает какое отмечать."""
    group_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        surveys = await surveys_svc.recent_closed_surveys(s, group_id, limit=5)
        title = format_group_title(group)
    if not surveys:
        await call.answer("Нет прошедших занятий", show_alert=True)
        return
    items = [(s.id, s.lesson_at.strftime("%d.%m в %H:%M")) for s in surveys]
    await call.message.edit_text(
        f"✏️ Какое занятие «{title}» отметить?",
        reply_markup=closed_surveys_keyboard(group_id, items),
    )
    await call.answer()


async def _render_attendance_roster(
    call: CallbackQuery, survey_id: int, group_id: int
) -> None:
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        survey = await surveys_svc.get_survey(s, survey_id)
        if survey is None or survey.group_id != group_id:
            await call.answer("Опрос не найден", show_alert=True)
            return
        roster = await surveys_svc.attendance_roster(s, survey_id)
        items = [
            (p, r.actually_attended, r.answer.value if r.answer else None)
            for p, r in roster
        ]
        title = format_group_title(group)
    head = (
        f"✏️ «{title}» — {survey.lesson_at:%d.%m в %H:%M}\n"
        f"Тап по имени — переключить: ☐ → ✅ → ❌ → ☐.\n"
        f"В скобках: ✓ обещал, ✗ отказался, — молчал."
    )
    await call.message.edit_text(
        head, reply_markup=attendance_mark_keyboard(survey_id, group_id, items)
    )


@router.callback_query(F.data.startswith("atnlesson:"))
async def cb_attendance_open_lesson(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    survey_id, group_id = int(parts[1]), int(parts[2])
    await _render_attendance_roster(call, survey_id, group_id)
    await call.answer()


@router.callback_query(F.data.startswith("atnwk:"))
async def cb_walk_in_pick(call: CallbackQuery) -> None:
    """Показываем persons преподавателя, которых ЕЩЁ нет в этом опросе."""
    parts = (call.data or "").split(":")
    survey_id, group_id = int(parts[1]), int(parts[2])
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        survey = await surveys_svc.get_survey(s, survey_id)
        if survey is None or survey.group_id != group_id:
            await call.answer("Опрос не найден", show_alert=True)
            return
        roster = await surveys_svc.attendance_roster(s, survey_id)
        roster_ids = {p.id for p, _ in roster}
        all_persons = await persons_svc.list_persons(s, group.admin_id)
        candidates = [p for p in all_persons if p.id not in roster_ids]
    if not candidates:
        await call.answer(
            "Все известные ученики уже в ростере. "
            "Запиши нового человека через ссылку-приглашение.",
            show_alert=True,
        )
        return
    await call.message.edit_text(
        "➕ Кого добавить как «был на занятии»?\n"
        "Здесь только люди, которых ты уже видел в других группах.",
        reply_markup=walk_in_picker_keyboard(survey_id, group_id, candidates),
    )
    await call.answer()


@router.callback_query(F.data.startswith("atnwkpick:"))
async def cb_walk_in_add(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    survey_id, person_id = int(parts[1]), int(parts[2])
    async with session_scope() as s:
        survey = await surveys_svc.get_survey(s, survey_id)
        if survey is None:
            await call.answer("Опрос не найден", show_alert=True)
            return
        group = await _accessible_group(s, survey.group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        person = await s.get(Person, person_id)
        if person is None or person.admin_id != group.admin_id:
            await call.answer("Не найдено", show_alert=True)
            return
        await persons_svc.add_to_group(
            s, group, person.tg_user_id, person.tg_username, person.full_name,
        )
        await surveys_svc.add_walk_in(s, survey_id, person_id)
        group_id = group.id
    await _render_attendance_roster(call, survey_id, group_id)
    await call.answer("Добавил")


@router.callback_query(F.data.startswith("atntog:"))
async def cb_attendance_toggle(call: CallbackQuery) -> None:
    """Переключение состояния: None → True → False → None."""
    parts = (call.data or "").split(":")
    survey_id, person_id = int(parts[1]), int(parts[2])
    async with session_scope() as s:
        survey = await surveys_svc.get_survey(s, survey_id)
        if survey is None:
            await call.answer("Опрос не найден", show_alert=True)
            return
        group = await _accessible_group(s, survey.group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        # Найдём текущее состояние
        from sqlalchemy import select as _sel

        from app.db.models import Response
        cur = (await s.execute(
            _sel(Response.actually_attended).where(
                Response.survey_id == survey_id, Response.person_id == person_id
            )
        )).scalar_one_or_none()
        # None → True → False → None
        new_value = True if cur is None else (False if cur is True else None)
        await surveys_svc.set_actual_attendance(s, survey_id, person_id, new_value)
        group_id = group.id
    await _render_attendance_roster(call, survey_id, group_id)
    await call.answer()


# ── cancel single lesson ────────────────────────────────────────────


@router.callback_query(F.data.startswith("grpcancel:"))
async def cb_lesson_cancel_pick(call: CallbackQuery) -> None:
    """Шаг 1: показываем список 5 ближайших занятий — preпод выбирает какое отменить."""
    group_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        upcoming = upcoming_lessons(group.weekdays, group.lesson_time, local_now(), n=5)
        if not upcoming:
            await call.answer("Нет ближайших занятий", show_alert=True)
            return
        items: list[tuple[int, str, bool]] = []
        for dt in upcoming:
            cancelled = await cancellations_svc.is_cancelled(s, group.id, dt)
            items.append((int(dt.timestamp()), f"{dt:%d.%m (%a) в %H:%M}", cancelled))
        title = format_group_title(group)
    await call.message.edit_text(
        f"Отменить какое занятие «{title}»?",
        reply_markup=pick_lesson_to_cancel_keyboard(group_id, items),
    )
    await call.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer()


@router.callback_query(F.data.startswith("grpcanceldt:"))
async def cb_lesson_cancel_confirm(call: CallbackQuery) -> None:
    """Шаг 2: confirmation выбранной даты."""
    parts = (call.data or "").split(":")
    group_id, ts = int(parts[1]), int(parts[2])
    lesson_at = datetime.fromtimestamp(ts)
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        if await cancellations_svc.is_cancelled(s, group.id, lesson_at):
            await call.answer("Это занятие уже отменено", show_alert=True)
            return
        title = format_group_title(group)
    await call.message.edit_text(
        f"Отменить занятие «{title}» {lesson_at:%d.%m в %H:%M}?\n\n"
        f"Бот не разошлёт опрос; всем кто уже ответил «приду» и саппортам "
        f"уйдёт уведомление об отмене.",
        reply_markup=confirm_cancel_lesson_keyboard(group_id, ts),
    )
    await call.answer()


@router.callback_query(F.data.startswith("grpcancelok:"))
async def cb_lesson_cancel(call: CallbackQuery, bot: Bot) -> None:
    """Шаг 3: пишем cancellation, закрываем survey/support, шлём уведомления."""
    parts = (call.data or "").split(":")
    group_id, ts = int(parts[1]), int(parts[2])
    lesson_at = datetime.fromtimestamp(ts)
    yes_tg_ids: list[int] = []
    support_tg_ids: list[int] = []
    title = ""
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        title = format_group_title(group)
        await cancellations_svc.cancel(s, group.id, lesson_at)
        # Если survey уже был разослан — закрываем и собираем «приду»-учеников.
        survey = await surveys_svc.find_attendance_survey(s, group.id, lesson_at)
        if survey is not None:
            if survey.status == SurveyStatus.open:
                survey.status = SurveyStatus.closed
                await s.flush()
            from sqlalchemy import select as _sel

            from app.db.models import Response
            rows = (await s.execute(
                _sel(Person.tg_user_id)
                .join(Response, Response.person_id == Person.id)
                .where(Response.survey_id == survey.id, Response.answer == Answer.yes)
            )).scalars().all()
            yes_tg_ids = list(rows)
            # SupportCall на это занятие тоже закрываем и уведомляем согласившихся.
            sc = await supports_svc.get_call_for_survey(s, survey.id)
            if sc is not None and sc.status != SupportCallStatus.closed:
                from app.db.models import SupportClaim
                sup_rows = (await s.execute(
                    _sel(Person.tg_user_id)
                    .join(SupportClaim, SupportClaim.person_id == Person.id)
                    .where(SupportClaim.support_call_id == sc.id)
                )).scalars().all()
                support_tg_ids = list(sup_rows)
                sc.status = SupportCallStatus.closed
                await s.flush()

    notice = f"⚠️ Занятие «{title}» {lesson_at:%d.%m в %H:%M} отменено."
    for tg in yes_tg_ids:
        try:
            await bot.send_message(tg, notice)
        except Exception as e:  # noqa: BLE001
            log.info("cancel-notice to %s failed: %s", tg, e)
    sup_notice = (
        f"⚠️ Помощь на занятии «{title}» {lesson_at:%d.%m в %H:%M} "
        f"больше не нужна — занятие отменено."
    )
    for tg in support_tg_ids:
        try:
            await bot.send_message(tg, sup_notice)
        except Exception as e:  # noqa: BLE001
            log.info("cancel-notice (support) to %s failed: %s", tg, e)

    await call.message.edit_text(
        f"🚫 Занятие «{title}» {lesson_at:%d.%m в %H:%M} отменено. "
        f"Уведомления отправлены ({len(yes_tg_ids)} ученикам, "
        f"{len(support_tg_ids)} саппортам)."
    )
    await call.answer()


# ── co-admin management ─────────────────────────────────────────────


async def _show_coadmins_menu(call: CallbackQuery, group_id: int) -> None:
    async with session_scope() as s:
        group = await _owned_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Только owner может управлять co-админами", show_alert=True)
            return
        coadmins = await access_svc.coadmins_of_group(s, group.id)
        title = format_group_title(group)
    lines = [f"🛠 Со-админы «{title}»"]
    if coadmins:
        lines.append("")
        for a in coadmins:
            lines.append(f"• {a.full_name}")
    else:
        lines.append("Сейчас никого нет.")
    lines.append("")
    lines.append(
        "Чтобы добавить — получи ссылку и перешли её кандидату. "
        "Он откроет ссылку, бот спросит подтверждение, и он станет co-админом."
    )
    if call.message:
        await call.message.edit_text(
            "\n".join(lines),
            reply_markup=coadmins_menu_keyboard(group_id, coadmins),
        )
    await call.answer()


@router.callback_query(F.data.startswith("grpcoa:"))
async def cb_group_coadmins(call: CallbackQuery) -> None:
    group_id = int(call.data.split(":", 1)[1])
    await _show_coadmins_menu(call, group_id)


@router.callback_query(F.data.startswith("coalink:"))
async def cb_coadmin_invite_link(call: CallbackQuery, bot: Bot) -> None:
    group_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        group = await _owned_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Только owner может приглашать co-админов", show_alert=True)
            return
        invite = await access_svc.create_coadmin_invite(s, group.id)
        code = invite.code
        title = format_group_title(group)
    link = await _coadmin_link(bot, code)
    await call.message.answer(
        f"🛠 Ссылка-приглашение co-админом группы «{title}» — действует 24 ч.\n\n"
        f"Перешли её одному человеку. По клику бот спросит подтверждение.\n\n"
        f"<code>{escape(link)}</code>",
        parse_mode=ParseMode.HTML,
    )
    await call.answer("Ссылка готова — пересылай")


@router.callback_query(F.data.startswith("coarem:"))
async def cb_coadmin_remove(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    group_id, admin_id = int(parts[1]), int(parts[2])
    async with session_scope() as s:
        group = await _owned_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Только owner может убирать co-админов", show_alert=True)
            return
        removed = await access_svc.revoke_access(s, group.id, admin_id)
    if not removed:
        await call.answer("Уже не co-админ", show_alert=True)
    else:
        await call.answer("Убран")
    await _show_coadmins_menu(call, group_id)


async def handle_coadmin_invite_start(message: Message, code: str) -> None:
    """Точка входа из student.join_start, когда payload начинается с `coadm_`.

    Показывает приглашённому кнопку «Принять» — ничего не пишем в БД до подтверждения."""
    async with session_scope() as s:
        invite = await access_svc.get_coadmin_invite(s, code)
        if invite is None or not access_svc.invite_is_valid(invite):
            await message.answer(
                "Эта ссылка устарела или уже использована. Попроси у owner'а новую."
            )
            return
        group = await groups_svc.get_group(s, invite.group_id)
        if group is None:
            await message.answer("Группа не найдена — возможно, удалена.")
            return
        title = format_group_title(group)
        summary = await _format_group_summary(s, group)
    await message.answer(
        f"🛠 Тебя приглашают co-админом группы «{title}».\n{summary}\n\n"
        f"Принимаешь?",
        reply_markup=coadmin_accept_keyboard(code),
    )


@router.callback_query(F.data.startswith("coaacc:"))
async def cb_coadmin_accept(call: CallbackQuery, bot: Bot) -> None:
    """Кандидат подтверждает приглашение — создаём Admin (если надо) и grant_access."""
    code = (call.data or "").split(":", 1)[1]
    tg = call.from_user
    if not tg:
        return
    async with session_scope() as s:
        invite = await access_svc.get_coadmin_invite(s, code)
        if invite is None or not access_svc.invite_is_valid(invite):
            if call.message:
                await call.message.edit_text(
                    "Эта ссылка устарела или уже использована. "
                    "Попроси у owner'а новую."
                )
            await call.answer()
            return
        admin = await admins_svc.get_or_create(
            s, tg.id, tg.username, tg.full_name or f"id{tg.id}"
        )
        await access_svc.grant_access(s, invite.group_id, admin.id)
        await access_svc.consume_coadmin_invite(s, invite, admin.id)
        group = await groups_svc.get_group(s, invite.group_id)
        title = format_group_title(group) if group else "—"
    try:
        await grant_admin_commands(bot, tg.id)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not set admin commands for %s: %s", tg.id, e)
    if call.message:
        await call.message.edit_text(
            f"✅ Готово, теперь ты co-админ группы «{title}». "
            f"Команда /groups покажет её."
        )
    await call.answer()


@router.callback_query(F.data == "coadecl")
async def cb_coadmin_decline(call: CallbackQuery) -> None:
    if call.message:
        await call.message.edit_text("Окей, приглашение отклонено.")
    await call.answer()


# ── support pool ─────────────────────────────────────────────────────


@router.message(Command("pool"))
@router.message(F.text == MENU_POOL)
async def cmd_pool(message: Message) -> None:
    tg = message.from_user
    if not tg:
        return
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, tg.id)
        if teacher is None:
            return
        people = await persons_svc.list_persons(s, teacher.id)
    if not people:
        await message.answer(
            "Пул саппортов пуст — сюда попадут ученики, когда запишутся. "
            "Потом тапом задашь, кого и с каким уровнем можно звать на подмену."
        )
        return
    await message.answer(
        "🆘 Личный пул саппортов. Тап по человеку задаёт уровень "
        "(1 → 2 → 3 → убрать из пула):",
        reply_markup=pool_keyboard(people),
    )


# ── edit display name ────────────────────────────────────────────────


@router.message(Command("name"))
@router.message(F.text == MENU_NAME)
async def cmd_edit_name(message: Message, state: FSMContext) -> None:
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
    current = teacher.full_name if teacher else persons_rows[0].full_name
    await state.set_state(EditName.waiting)
    await message.answer(
        f"Как тебя называть в опросах и списках? Напиши имя одной строкой."
        f"\nСейчас: «{current}»"
    )


@router.message(EditName.waiting, F.text, ~F.text.in_(MENU_TEXTS))
async def edit_name_typed(message: Message, state: FSMContext) -> None:
    tg = message.from_user
    if not tg:
        return
    new_name = (message.text or "").strip()
    if not new_name:
        await message.answer("Пустое имя — напиши ещё раз.")
        return
    if len(new_name) > 200:
        new_name = new_name[:200]
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, tg.id)
        if teacher is not None:
            await admins_svc.set_name(s, teacher.id, new_name)
        touched = await persons_svc.set_name_everywhere(s, tg.id, new_name)
    await state.clear()
    if teacher is not None and touched == 0:
        await message.answer(f"✅ Записал: тебя зовут «{new_name}».")
    elif teacher is not None:
        await message.answer(
            f"✅ Записал: тебя зовут «{new_name}». Обновил в {touched} карточке(ах)."
        )
    else:
        await message.answer(
            f"✅ Записал: тебя зовут «{new_name}». Преподаватели увидят это имя."
        )


# ── support pool ─────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("tpool:"))
async def cb_pool_toggle(call: CallbackQuery) -> None:
    person_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, call.from_user.id)
        person = await s.get(Person, person_id)
        if teacher is None or person is None or person.admin_id != teacher.id:
            await call.answer("Не найдено", show_alert=True)
            return
        current = person.support_level
        new_level = None if current == _MAX_LEVEL else (current or 0) + 1
        await persons_svc.set_support_level(s, person_id, new_level)
        people = await persons_svc.list_persons(s, teacher.id)
    if call.message:
        await call.message.edit_reply_markup(reply_markup=pool_keyboard(people))
    await call.answer()
