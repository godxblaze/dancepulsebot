from __future__ import annotations

import logging
from datetime import datetime, timedelta
from html import escape

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
from app.core.config import settings
from app.db.models import Gender, Group
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import cancellations as cancellations_svc
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import schools as schools_svc
from app.services import venues as venues_svc
from app.services.schedule import (
    format_lesson_at,
    format_weekdays,
    next_lesson_at,
    upcoming_lessons,
    utc_now,
)

log = logging.getLogger(__name__)
router = Router()

JOIN_PREFIX = "join_"
PARTNER_PREFIX = "partner_"
SUPPORT_PREFIX = "support_"


async def _notify_support_added(bot: Bot, admin_tg: int, name: str) -> None:
    try:
        await bot.send_message(
            admin_tg, f"🆘 {name} готов(а) выручать — добавил(а) в твой пул саппортов."
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Could not notify admin %s about support: %s", admin_tg, e)


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
    if payload.startswith(SUPPORT_PREFIX):
        await _support_invite_start(message, payload[len(SUPPORT_PREFIX):], state, bot)
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


async def _support_invite_start(
    message: Message, code: str, state: FSMContext, bot: Bot
) -> None:
    """Онбординг саппорта по персональной ссылке препода: Person в школе БЕЗ
    группы → сразу в пул пригласившего препода (уровень 1)."""
    tg = message.from_user
    async with session_scope() as s:
        admin = await admins_svc.get_by_support_code(s, code)
        school = await schools_svc.get_owned_school(s, admin.id) if admin else None
        if school is None and admin is not None:
            # Co-admin без собственной школы — саппорта записываем в первую
            # школу, где он member (иначе ссылка мертва для наёмных преподов).
            memberships = await schools_svc.admin_schools(s, admin.id)
            school = memberships[0][0] if memberships else None
        if admin is None or school is None:
            await message.answer("Ссылка-приглашение саппорта не найдена или устарела.")
            return
        existing = await persons_svc.get_person(s, school.id, tg.id)
        known = existing is not None and existing.gender is not None
        admin_id, admin_tg, school_id = admin.id, admin.tg_user_id, school.id
        if known:
            await persons_svc.set_pool_level(s, admin_id, existing.id, 1)
            known_name = existing.full_name

    if known:
        await _notify_support_added(bot, admin_tg, known_name)
        await message.answer(
            "✅ Ты в пуле саппортов — препод позовёт, если на занятии не хватит пары."
        )
        return

    await state.set_state(JoinFlow.name)
    await state.update_data(support_admin_id=admin_id, support_school_id=school_id)
    await message.answer(
        "Привет! Запишу тебя саппортом — буду звать выручить, когда на занятии "
        "не хватает пары.\n\nКак тебя зовут?",
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

    # Ветка онбординга саппорта (по support-ссылке): нет группы, сразу в пул.
    if "support_admin_id" in data:
        from app.db.models import Admin
        async with session_scope() as s:
            person = await persons_svc.ensure_person(
                s, data["support_school_id"], tg.id, tg.username, name
            )
            await persons_svc.set_gender(s, person.id, gender)
            await persons_svc.set_pool_level(s, data["support_admin_id"], person.id, 1)
            inviter = await s.get(Admin, data["support_admin_id"])
            inviter_tg = inviter.tg_user_id if inviter else None
        await state.clear()
        if call.message:
            await call.message.edit_text(
                f"✅ Готово, {name}! Ты в пуле саппортов — позову, когда на "
                f"занятии не хватит пары."
            )
        await call.answer()
        if inviter_tg is not None:
            await _notify_support_added(bot, inviter_tg, name)
        return

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


async def _nearest_lesson(
    session, groups: list[Group], tz_map: dict[int, str]
) -> tuple[Group, datetime] | None:
    """The soonest upcoming, NOT cancelled lesson across the given groups.
    tz_map: school_id → tz. Отменённые инстансы пропускаем — «Ближайшее» не
    должно показывать занятие, которого не будет."""
    now = utc_now()
    best: tuple[Group, datetime] | None = None
    for g in groups:
        tz_name = tz_map.get(g.school_id) if g.school_id else None
        for dt in upcoming_lessons(g.weekdays, g.lesson_time, now, n=4, tz=tz_name):
            if await cancellations_svc.is_cancelled(session, g.id, dt):
                continue
            if best is None or dt < best[1]:
                best = (g, dt)
            break
    return best


async def show_student_home(message: Message) -> None:
    """Personal screen for a student: their groups and next lesson."""
    tg = message.from_user
    if not tg:
        return
    async with session_scope() as s:
        person = await persons_svc.find_by_tg(s, tg.id)
        if person is None:
            contact = settings.developer_contact.strip()
            if contact:
                teacher_line = (
                    "<b>Если ты преподаватель или администратор школы</b> — "
                    f"напиши {escape(contact)}, он подключит тебя. "
                    f"Твой Telegram ID, если попросят: <code>{tg.id}</code>."
                )
            else:
                teacher_line = (
                    "<b>Если ты преподаватель или администратор школы</b> — "
                    "передай свой Telegram ID администратору бота: "
                    f"<code>{tg.id}</code>. Он выдаст доступ, и сможешь создать "
                    "свою школу."
                )
            await message.answer(
                "👋 <b>Это Dance Pulse</b> — бот посещаемости для танцевальных школ.\n\n"
                "Перед каждым занятием спрашиваю учеников «придёшь?» и показываю "
                "преподавателю готовую сводку. Когда в группе перекос по полу — "
                "сам собираю саппортов из пула.\n\n"
                "<b>Если ты ученик</b> — попроси у своего преподавателя ссылку-"
                "приглашение или QR-код. Без ссылки я не знаю в какую группу "
                "тебя записать.\n\n"
                + teacher_line,
                parse_mode=ParseMode.HTML,
                reply_markup=onboarding_menu_keyboard(),
            )
            return
        groups = await persons_svc.groups_for_tg(s, tg.id)
        name = person.full_name
        tz_map: dict[int, str] = {}
        for g in groups:
            if g.school_id is not None and g.school_id not in tz_map:
                tz_map[g.school_id] = await schools_svc.get_timezone(s, g.school_id)
        nearest = await _nearest_lesson(s, groups, tz_map) if groups else None

    lines = [f"👋 Привет, {name}!"]
    if groups:
        lines.append("\nТвои группы:")
        for g in groups:
            lines.append(
                f"• «{format_group_title(g)}» — "
                f"{format_weekdays(g.weekdays)} в {g.lesson_time}"
            )
        if nearest is not None:
            g, dt = nearest
            tz_name = tz_map.get(g.school_id) if g.school_id else None
            lines.append(
                f"\n📅 Ближайшее: «{format_group_title(g)}» — "
                f"{format_lesson_at(dt, '%d.%m в %H:%M', tz=tz_name)}"
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
        groups = await persons_svc.groups_for_tg(s, tg.id)
        tz_map: dict[int, str] = {}
        for g in groups:
            if g.school_id is not None and g.school_id not in tz_map:
                tz_map[g.school_id] = await schools_svc.get_timezone(s, g.school_id)

    from app.services.schedule import school_tz_zone

    now = utc_now()
    blocks: list[str] = []
    async with session_scope() as s:
        for g in groups:
            tz_name = tz_map.get(g.school_id) if g.school_id else None
            tz = school_tz_zone(tz_name)
            today_local = now.astimezone(tz).date()
            tomorrow_local = today_local + timedelta(days=1)
            dt = next_lesson_at(g.weekdays, g.lesson_time, now, tz=tz_name)
            if dt is None:
                continue
            dt_local_date = dt.astimezone(tz).date()
            if dt_local_date not in (today_local, tomorrow_local):
                continue
            when = "сегодня" if dt_local_date == today_local else "завтра"
            if await cancellations_svc.is_cancelled(s, g.id, dt):
                blocks.append(
                    f"🚫 «{format_group_title(g)}» — {when} в {g.lesson_time} "
                    "— занятие отменено"
                )
                continue
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
