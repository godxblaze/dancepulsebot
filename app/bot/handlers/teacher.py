from __future__ import annotations

import logging
import re
from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.bot.keyboards import (
    confirm_delete_keyboard,
    group_actions_keyboard,
    groups_keyboard,
    pool_keyboard,
    weekdays_keyboard,
)
from app.bot.states import NewGroup
from app.bot.utils import get_bot_username, qr_png
from app.core.config import settings
from app.db.models import Group, Person, SupportScope
from app.db.session import session_scope
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import surveys as surveys_svc
from app.services import teachers as teachers_svc
from app.services.schedule import format_weekdays, local_now, next_lesson_at

log = logging.getLogger(__name__)
router = Router()

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

_HELP = (
    "💓 Dance Pulse — бот опросов посещаемости\n\n"
    "Перед каждым занятием бот спрашивает учеников группы, придут ли они, "
    "и показывает тебе сводку.\n\n"
    "Команды:\n"
    "/newgroup — создать группу\n"
    "/groups — мои группы\n"
    "/today — сводка по ближайшим занятиям\n"
    "/stats — посещаемость за последние занятия\n"
    "/pool — пул саппортов на подмену\n"
    "/help — эта справка"
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
    full_name = tg.full_name or tg.username or f"id{tg.id}"
    async with session_scope() as s:
        await teachers_svc.get_or_create(s, tg.id, tg.username, full_name)
    await message.answer(f"Привет, {full_name}!\n\n{_HELP}")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(_HELP)


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    tg = message.from_user
    if not tg:
        return
    now = local_now()
    today = now.date()
    tomorrow = today + timedelta(days=1)
    blocks: list[str] = []

    async with session_scope() as s:
        teacher = await teachers_svc.get_by_tg_id(s, tg.id)
        groups = await groups_svc.teacher_groups(s, teacher.id) if teacher else []
        for g in groups:
            lesson_at = next_lesson_at(g.weekdays, g.lesson_time, now)
            if lesson_at is None or lesson_at.date() not in (today, tomorrow):
                continue
            when = "сегодня" if lesson_at.date() == today else "завтра"
            survey = await surveys_svc.find_attendance_survey(s, g.id, lesson_at)
            if survey is None:
                blocks.append(
                    f"📋 «{g.name}» — {when} в {g.lesson_time}\n"
                    f"   опрос ещё не разослан (придёт ближе к занятию)"
                )
                continue
            summary = await surveys_svc.summarize(s, survey.id)
            lines = [
                f"📋 «{g.name}» — {when} в {g.lesson_time}",
                f"   придут: {summary.yes} из {summary.total}",
            ]
            if summary.no_names:
                lines.append(f"   не придут: {', '.join(summary.no_names)}")
            if summary.pending_names:
                lines.append(f"   не ответили: {', '.join(summary.pending_names)}")
            blocks.append("\n".join(lines))

    if not blocks:
        await message.answer("На сегодня и завтра занятий нет.")
    else:
        await message.answer("\n\n".join(blocks))


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    tg = message.from_user
    if not tg:
        return
    blocks: list[str] = []

    async with session_scope() as s:
        teacher = await teachers_svc.get_by_tg_id(s, tg.id)
        groups = await groups_svc.teacher_groups(s, teacher.id) if teacher else []
        if not groups:
            await message.answer("У тебя пока нет групп. Создай первую: /newgroup")
            return
        for g in groups:
            closed = await surveys_svc.recent_closed_surveys(s, g.id, limit=5)
            if not closed:
                blocks.append(f"📊 «{g.name}» — пока нет завершённых опросов")
                continue
            lines = [f"📊 «{g.name}» — последние {len(closed)} занятий (по ответам на опрос):"]
            total_yes = total_all = 0
            for survey in reversed(closed):  # oldest first
                summary = await surveys_svc.summarize(s, survey.id)
                total_yes += summary.yes
                total_all += summary.total
                lines.append(f"   {survey.lesson_at:%d.%m}: {summary.yes} из {summary.total}")
            if total_all:
                pct = round(100 * total_yes / total_all)
                lines.append(f"   средний отклик «приду»: {pct}%")
            blocks.append("\n".join(lines))

    await message.answer("\n\n".join(blocks))


# ── new group wizard ─────────────────────────────────────────────────


@router.message(Command("newgroup"))
async def newgroup_start(message: Message, state: FSMContext) -> None:
    await state.set_state(NewGroup.name)
    await message.answer("Название новой группы? (например: Hip-Hop начинающие)")


@router.message(NewGroup.name, F.text)
async def newgroup_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым. Попробуй ещё раз.")
        return
    await state.update_data(name=name, weekdays=[])
    await state.set_state(NewGroup.weekdays)
    await message.answer(
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
        await call.message.edit_text("Во сколько начинается занятие? Формат ЧЧ:ММ, например 19:00")
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


@router.message(NewGroup.lesson_time, F.text)
async def newgroup_time(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = (message.text or "").strip()
    m = _TIME_RE.match(raw)
    if not m:
        await message.answer("Не похоже на время. Нужен формат ЧЧ:ММ, например 19:00")
        return
    lesson_time = f"{int(m.group(1)):02d}:{m.group(2)}"

    data = await state.get_data()
    tg = message.from_user
    assert tg is not None
    weekdays = ",".join(str(d) for d in data["weekdays"])

    async with session_scope() as s:
        teacher = await teachers_svc.get_or_create(
            s, tg.id, tg.username, tg.full_name or f"id{tg.id}"
        )
        group = await groups_svc.create_group(
            s, teacher.id, data["name"], weekdays, lesson_time, settings.default_lead_hours
        )
        code = group.join_code
        name = group.name

    await state.clear()
    link = await _join_link(bot, code)
    await message.answer(
        f"✅ Группа «{name}» создана.\n"
        f"Расписание: {format_weekdays(weekdays)} в {lesson_time}\n\n"
        f"Перешли эту ссылку ученикам — по ней они запишутся в группу:\n{link}"
    )


# ── groups list & actions ────────────────────────────────────────────


async def _owned_group(session, group_id: int, tg_user_id: int) -> Group | None:
    teacher = await teachers_svc.get_by_tg_id(session, tg_user_id)
    if teacher is None:
        return None
    group = await groups_svc.get_group(session, group_id)
    if group is None or group.teacher_id != teacher.id:
        return None
    return group


async def _send_groups_list(target: Message) -> None:
    tg = target.chat
    async with session_scope() as s:
        teacher = await teachers_svc.get_by_tg_id(s, tg.id)
        groups = await groups_svc.teacher_groups(s, teacher.id) if teacher else []
    if not groups:
        await target.answer("У тебя пока нет групп. Создай первую: /newgroup")
        return
    await target.answer("Твои группы:", reply_markup=groups_keyboard(groups))


@router.message(Command("groups"))
async def cmd_groups(message: Message) -> None:
    await _send_groups_list(message)


@router.callback_query(F.data == "grplist")
async def cb_groups_list(call: CallbackQuery) -> None:
    async with session_scope() as s:
        teacher = await teachers_svc.get_by_tg_id(s, call.from_user.id)
        groups = await groups_svc.teacher_groups(s, teacher.id) if teacher else []
    if not groups:
        await call.message.edit_text("У тебя пока нет групп. Создай первую: /newgroup")
    else:
        await call.message.edit_text("Твои группы:", reply_markup=groups_keyboard(groups))
    await call.answer()


@router.callback_query(F.data.startswith("grp:"))
async def cb_group_card(call: CallbackQuery) -> None:
    group_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        group = await _owned_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        count = await persons_svc.count_members(s, group_id)
        text = (
            f"📋 «{group.name}»\n"
            f"Расписание: {format_weekdays(group.weekdays)} в {group.lesson_time}\n"
            f"Опрос приходит за {group.lead_hours} ч до занятия\n"
            f"Учеников: {count}"
        )
    await call.message.edit_text(text, reply_markup=group_actions_keyboard(group_id))
    await call.answer()


@router.callback_query(F.data.startswith("grpstu:"))
async def cb_group_students(call: CallbackQuery) -> None:
    group_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        group = await _owned_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        members = await persons_svc.group_members(s, group_id)
    if members:
        lines = "\n".join(f"  • {p.full_name}" for p in members)
        text = f"👥 Ученики «{group.name}» ({len(members)}):\n{lines}"
    else:
        text = f"👥 В группе «{group.name}» пока нет учеников. Перешли им ссылку-приглашение."
    await call.message.edit_text(text, reply_markup=group_actions_keyboard(group_id))
    await call.answer()


@router.callback_query(F.data.startswith("grplink:"))
async def cb_group_link(call: CallbackQuery, bot: Bot) -> None:
    group_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        group = await _owned_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        code = group.join_code
        name = group.name
    link = await _join_link(bot, code)
    await call.message.edit_text(
        f"🔗 Ссылка-приглашение в «{name}»:\n{link}\n\nПерешли её ученикам.",
        reply_markup=group_actions_keyboard(group_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("grpqr:"))
async def cb_group_qr(call: CallbackQuery, bot: Bot) -> None:
    group_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        group = await _owned_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        code = group.join_code
        name = group.name
    link = await _join_link(bot, code)
    await call.message.answer_photo(
        BufferedInputFile(qr_png(link), filename="qr.png"),
        caption=(
            f"📷 QR-код группы «{name}».\n"
            f"Покажи новому ученику или распечатай — он наведёт камеру и сразу запишется."
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith("grpdel:"))
async def cb_group_delete_confirm(call: CallbackQuery) -> None:
    group_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        group = await _owned_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        name = group.name
    await call.message.edit_text(
        f"Удалить группу «{name}» вместе со списком учеников?",
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
        name = group.name
        await groups_svc.delete_group(s, group)
    await call.message.edit_text(f"🗑 Группа «{name}» удалена.")
    await call.answer()


# ── support pool ─────────────────────────────────────────────────────


@router.message(Command("pool"))
async def cmd_pool(message: Message) -> None:
    tg = message.from_user
    if not tg:
        return
    async with session_scope() as s:
        teacher = await teachers_svc.get_by_tg_id(s, tg.id)
        people = await persons_svc.list_persons(s, teacher.id) if teacher else []
        scope = teacher.support_scope if teacher else SupportScope.own_and_open
    if not people:
        await message.answer(
            "Пул саппортов пуст — сюда попадут ученики, когда запишутся. "
            "Потом отметишь, кого можно звать на подмену."
        )
        return
    await message.answer(
        "🆘 Личный пул саппортов — отметь, кого можно звать выручать другие группы:",
        reply_markup=pool_keyboard(people, scope),
    )


@router.callback_query(F.data.startswith("tpool:"))
async def cb_pool_toggle(call: CallbackQuery) -> None:
    person_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        teacher = await teachers_svc.get_by_tg_id(s, call.from_user.id)
        person = await s.get(Person, person_id)
        if teacher is None or person is None or person.teacher_id != teacher.id:
            await call.answer("Не найдено", show_alert=True)
            return
        await persons_svc.set_teacher_pool(s, person_id, not person.in_teacher_pool)
        people = await persons_svc.list_persons(s, teacher.id)
        scope = teacher.support_scope
    if call.message:
        await call.message.edit_reply_markup(reply_markup=pool_keyboard(people, scope))
    await call.answer()


@router.callback_query(F.data == "sscope:toggle")
async def cb_scope_toggle(call: CallbackQuery) -> None:
    async with session_scope() as s:
        teacher = await teachers_svc.get_by_tg_id(s, call.from_user.id)
        if teacher is None:
            await call.answer()
            return
        scope = await teachers_svc.toggle_support_scope(s, teacher.id)
        people = await persons_svc.list_persons(s, teacher.id)
    if call.message:
        await call.message.edit_reply_markup(reply_markup=pool_keyboard(people, scope))
    await call.answer()
