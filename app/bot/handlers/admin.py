from __future__ import annotations

import logging
import re
from datetime import timedelta
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
    confirm_delete_keyboard,
    confirm_remove_from_group_keyboard,
    group_actions_keyboard,
    group_edit_menu_keyboard,
    groups_keyboard,
    level_picker_keyboard,
    person_card_keyboard,
    pool_keyboard,
    skip_keyboard,
    students_keyboard,
    style_picker_keyboard,
    style_typing_keyboard,
    today_ping_keyboard,
    venue_picker_keyboard,
    weekdays_keyboard,
)
from app.bot.states import EditGroup, EditName, NewGroup
from app.bot.utils import (
    bar,
    derive_group_name,
    format_group_title,
    get_bot_username,
    qr_png,
)
from app.core.config import settings
from app.db.models import Gender, Group, Person, SurveyStatus
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import group_access as access_svc
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import surveys as surveys_svc
from app.services import venues as venues_svc
from app.services.schedule import format_weekdays, local_now, next_lesson_at

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
                lines.append(
                    f"{bar(summary.yes, summary.total)}  "
                    f"{survey.lesson_at:%d.%m} · {summary.yes}/{summary.total}"
                )
            if total_all:
                pct = round(100 * total_yes / total_all)
                lines.append(f"средний отклик: <b>{pct}%</b>")
            blocks.append("\n".join(lines))

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
        count = await persons_svc.count_members(s, group_id)
        summary = await _format_group_summary(s, group)
        title = format_group_title(group)
        text = (
            f"📋 «{title}»\n"
            f"{summary}\n"
            f"Опрос приходит за {group.lead_hours} ч до занятия\n"
            f"Учеников: {count}"
        )
    await call.message.edit_text(text, reply_markup=group_actions_keyboard(group_id))
    await call.answer()


@router.callback_query(F.data.startswith("grpstu:"))
async def cb_group_students(call: CallbackQuery) -> None:
    group_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        group = await _accessible_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        members = await persons_svc.group_members(s, group_id)
    title = format_group_title(group)
    if members:
        text = f"👥 Ученики «{title}» ({len(members)}). Тап по имени — карточка:"
        await call.message.edit_text(text, reply_markup=students_keyboard(group_id, members))
    else:
        text = f"👥 В группе «{title}» пока нет учеников. Перешли им ссылку-приглашение."
        await call.message.edit_text(text, reply_markup=group_actions_keyboard(group_id))
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
        reply_markup=person_card_keyboard(group_id, person_id, person.support_level),
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
        reply_markup=person_card_keyboard(group_id, person_id, person.support_level),
    )
    await call.answer("Уровень обновлён")


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
        text = f"👥 В группе «{group_name}» больше никого нет. Перешли ссылку-приглашение."
        await call.message.edit_text(text, reply_markup=group_actions_keyboard(group_id))
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
