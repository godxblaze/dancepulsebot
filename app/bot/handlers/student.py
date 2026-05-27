from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot.keyboards import (
    gender_keyboard,
    join_name_keyboard,
    onboarding_menu_keyboard,
    student_menu_keyboard,
)
from app.bot.states import JoinFlow
from app.bot.utils import format_group_title, format_place
from app.db.models import Gender, Group
from app.db.session import session_scope
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import schools as schools_svc
from app.services import venues as venues_svc
from app.services.schedule import format_lesson_at, format_weekdays, next_lesson_at, utc_now

log = logging.getLogger(__name__)
router = Router()

JOIN_PREFIX = "join_"
PARTNER_PREFIX = "partner_"


def _display_name(tg) -> str:
    return tg.full_name or tg.username or f"id{tg.id}"


async def _notify_admin(
    bot: Bot, group: Group, person_name: str, person_id: int | None = None
) -> None:
    """Сообщить всем admin'ам школы группы что записался ученик."""
    if group.school_id is None:
        return
    async with session_scope() as s:
        tg_ids = await schools_svc.member_tg_ids(s, group.school_id)
    if not tg_ids:
        return
    reply_markup = None
    if person_id is not None:
        reply_markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="👤 Открыть карточку",
                    callback_data=f"pcard:{group.id}:{person_id}",
                )]
            ]
        )
    text = f"🔔 {person_name} записал(ась) в группу «{format_group_title(group)}»."
    for tg_id in tg_ids:
        try:
            await bot.send_message(tg_id, text, reply_markup=reply_markup)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not notify admin %s: %s", tg_id, e)


@router.message(CommandStart(deep_link=True))
async def join_start(
    message: Message, command: CommandObject, state: FSMContext, bot: Bot
) -> None:
    payload = (command.args or "").strip()
    tg = message.from_user
    if not tg:
        return
    if payload.startswith(PARTNER_PREFIX):
        # Приглашение co-admin'ом в школу — обрабатывает admin.py с подтверждением.
        from app.bot.handlers.school import handle_school_invite_start

        await handle_school_invite_start(message, payload[len(PARTNER_PREFIX):])
        return
    if not payload.startswith(JOIN_PREFIX):
        await message.answer(
            "Ссылка-приглашение не распознана. Попроси преподавателя прислать новую."
        )
        return

    code = payload[len(JOIN_PREFIX):]
    known_name = ""
    async with session_scope() as s:
        group = await groups_svc.get_by_join_code(s, code)
        if group is None:
            await message.answer("Группа не найдена — возможно, ссылка устарела.")
            return
        if await persons_svc.get_in_group(s, group.id, tg.id) is not None:
            await message.answer(f"Ты уже в группе «{format_group_title(group)}». Всё в порядке.")
            return
        person = await persons_svc.get_person(s, group.school_id, tg.id)
        known = person is not None and person.gender is not None
        known_person_id: int | None = None
        if known:
            joined_person, _ = await persons_svc.add_to_group(
                s, group, tg.id, tg.username, person.full_name
            )
            known_name = person.full_name
            known_person_id = joined_person.id

    if known:
        await _notify_admin(bot, group, known_name, known_person_id)
        await message.answer(
            f"✅ Записал тебя в группу «{format_group_title(group)}». "
            f"Перед занятием спрошу, придёшь ли."
        )
        return

    # New face — ask name, then gender.
    await state.set_state(JoinFlow.name)
    await state.update_data(group_id=group.id)
    await message.answer(
        f"Привет! Записываю тебя в группу «{format_group_title(group)}».\n\nКак тебя зовут?",
        reply_markup=join_name_keyboard(_display_name(tg)),
    )


@router.callback_query(JoinFlow.name, F.data == "join:name_ok")
async def join_name_confirm(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(name=_display_name(call.from_user))
    await state.set_state(JoinFlow.gender)
    if call.message:
        await call.message.edit_text(
            "Отлично! Ты парень или девушка?", reply_markup=gender_keyboard()
        )
    await call.answer()


@router.message(JoinFlow.name, F.text)
async def join_name_typed(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("Имя не может быть пустым. Напиши, как тебя записать.")
        return
    await state.update_data(name=name)
    await state.set_state(JoinFlow.gender)
    await message.answer(
        "Отлично! Ты парень или девушка?", reply_markup=gender_keyboard()
    )


@router.callback_query(JoinFlow.gender, F.data.startswith("join:gender:"))
async def join_gender(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    gender = Gender.male if (call.data or "").endswith(":male") else Gender.female
    data = await state.get_data()
    tg = call.from_user
    name = data.get("name") or _display_name(tg)

    async with session_scope() as s:
        group = await groups_svc.get_group(s, data["group_id"])
        if group is None:
            await state.clear()
            if call.message:
                await call.message.edit_text("Группа не найдена — возможно, удалена.")
            await call.answer()
            return
        person, _ = await persons_svc.add_to_group(s, group, tg.id, tg.username, name)
        await persons_svc.set_gender(s, person.id, gender)
        person_id = person.id

    await state.clear()
    if call.message:
        await call.message.edit_text(
            f"✅ Готово, {name}! Перед каждым занятием я спрошу, придёшь ли ты — "
            f"просто жми кнопку."
        )
    await call.answer()
    await _notify_admin(bot, group, name, person_id)


def _nearest_lesson(groups: list[Group]) -> tuple[Group, datetime] | None:
    """The soonest upcoming lesson across the given groups."""
    now = utc_now()
    best: tuple[Group, datetime] | None = None
    for g in groups:
        dt = next_lesson_at(g.weekdays, g.lesson_time, now)
        if dt is not None and (best is None or dt < best[1]):
            best = (g, dt)
    return best


async def show_student_home(message: Message) -> None:
    """Personal screen for a student: their groups and next lesson."""
    tg = message.from_user
    if not tg:
        return
    async with session_scope() as s:
        person = await persons_svc.find_by_tg(s, tg.id)
        if person is None:
            await message.answer(
                "👋 <b>Это Dance Pulse</b> — бот посещаемости для танцевальных школ.\n\n"
                "Перед каждым занятием спрашиваю учеников «придёшь?» и показываю "
                "преподавателю готовую сводку. Когда в группе перекос по полу — "
                "сам собираю саппортов из пула.\n\n"
                "<b>Если ты ученик</b> — попроси у своего преподавателя ссылку-"
                "приглашение или QR-код. Без ссылки я не знаю в какую группу "
                "тебя записать.\n\n"
                "<b>Если ты преподаватель или администратор школы</b> — передай "
                f"свой Telegram ID разработчику: <code>{tg.id}</code>. "
                "Он выдаст тебе админ-статус, и сможешь создать свою школу.",
                parse_mode=ParseMode.HTML,
                reply_markup=onboarding_menu_keyboard(),
            )
            return
        groups = await persons_svc.person_groups(s, person.id)
        name = person.full_name

    lines = [f"👋 Привет, {name}!"]
    if groups:
        lines.append("\nТвои группы:")
        for g in groups:
            lines.append(
                f"• «{format_group_title(g)}» — "
                f"{format_weekdays(g.weekdays)} в {g.lesson_time}"
            )
        nearest = _nearest_lesson(groups)
        if nearest is not None:
            g, dt = nearest
            lines.append(
                f"\n📅 Ближайшее: «{format_group_title(g)}» — "
                f"{format_lesson_at(dt, '%d.%m в %H:%M')}"
            )
    else:
        lines.append("\nТы пока не записан ни в одну группу.")
    lines.append("\nПеред каждым занятием я спрошу, придёшь ли ты.")
    await message.answer("\n".join(lines), reply_markup=student_menu_keyboard())


async def show_student_today(message: Message) -> None:
    """Student's 'today' screen — nearest lesson within today/tomorrow."""
    tg = message.from_user
    if not tg:
        return
    async with session_scope() as s:
        person = await persons_svc.find_by_tg(s, tg.id)
        if person is None:
            await message.answer(
                "Ты пока не записан ни в одну группу — попроси у преподавателя ссылку-приглашение.",
                reply_markup=student_menu_keyboard(),
            )
            return
        groups = await persons_svc.person_groups(s, person.id)

    now = utc_now()
    today = now.date()
    tomorrow_d = (now + timedelta(days=1)).date()
    blocks: list[str] = []
    async with session_scope() as s:
        for g in groups:
            dt = next_lesson_at(g.weekdays, g.lesson_time, now)
            if dt is None or dt.date() not in (today, tomorrow_d):
                continue
            when = "сегодня" if dt.date() == today else "завтра"
            line = f"👯 «{format_group_title(g)}» — {when} в {g.lesson_time}"
            venue = (
                await venues_svc.get_venue(s, g.venue_id)
                if g.venue_id is not None
                else None
            )
            place = format_place(g, venue)
            if place:
                line += f"\n   📍 {place}"
            blocks.append(line)
    if not blocks:
        await message.answer(
            "На сегодня и завтра занятий нет.", reply_markup=student_menu_keyboard()
        )
        return
    await message.answer("\n".join(blocks), reply_markup=student_menu_keyboard())
