from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import gender_keyboard, join_name_keyboard
from app.bot.states import JoinFlow
from app.db.models import Gender, Group, Teacher
from app.db.session import session_scope
from app.services import groups as groups_svc
from app.services import persons as persons_svc

log = logging.getLogger(__name__)
router = Router()

JOIN_PREFIX = "join_"


def _display_name(tg) -> str:
    return tg.full_name or tg.username or f"id{tg.id}"


async def _notify_teacher(bot: Bot, group: Group, person_name: str) -> None:
    """Tell the teacher that a person just joined one of their groups."""
    async with session_scope() as s:
        teacher = await s.get(Teacher, group.teacher_id)
    if teacher is None:
        return
    try:
        await bot.send_message(
            teacher.tg_user_id,
            f"🔔 {person_name} записал(ась) в группу «{group.name}».",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("Could not notify teacher %s: %s", teacher.tg_user_id, e)


@router.message(CommandStart(deep_link=True))
async def join_start(
    message: Message, command: CommandObject, state: FSMContext, bot: Bot
) -> None:
    payload = (command.args or "").strip()
    tg = message.from_user
    if not tg:
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
            await message.answer(f"Ты уже в группе «{group.name}». Всё в порядке.")
            return
        person = await persons_svc.get_person(s, group.teacher_id, tg.id)
        known = person is not None and person.gender is not None
        if known:
            await persons_svc.add_to_group(
                s, group, tg.id, tg.username, person.full_name
            )
            known_name = person.full_name

    if known:
        await _notify_teacher(bot, group, known_name)
        await message.answer(
            f"✅ Записал тебя в группу «{group.name}». Перед занятием спрошу, придёшь ли."
        )
        return

    # New face — ask name, then gender.
    await state.set_state(JoinFlow.name)
    await state.update_data(group_id=group.id)
    await message.answer(
        f"Привет! Записываю тебя в группу «{group.name}».\n\nКак тебя зовут?",
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

    await state.clear()
    if call.message:
        await call.message.edit_text(
            f"✅ Готово, {name}! Перед каждым занятием я спрошу, придёшь ли ты — "
            f"просто жми кнопку."
        )
    await call.answer()
    await _notify_teacher(bot, group, name)
