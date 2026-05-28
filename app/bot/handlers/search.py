"""Поиск по имени в длинных списках людей: walk-in / pool / ростер группы.

Кнопка «🔍 Поиск» в этих клавиатурах кидает `search:<kind>[:ctx...]`. Здесь:
ставим FSM EntitySearch.query (запоминаем kind+контекст), на следующий текст
фильтруем нужный список по подстроке имени и перерисовываем ту же клавиатуру.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers._access import accessible_group
from app.bot.keyboards import (
    pool_keyboard,
    students_keyboard,
    walk_in_picker_keyboard,
)
from app.bot.states import EntitySearch
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import persons as persons_svc
from app.services import surveys as surveys_svc

router = Router()


@router.callback_query(F.data.startswith("search:"))
async def cb_search_start(call: CallbackQuery, state: FSMContext) -> None:
    parts = (call.data or "").split(":")
    kind, ctx = parts[1], parts[2:]
    await state.set_state(EntitySearch.query)
    await state.update_data(search_kind=kind, search_ctx=ctx)
    if call.message:
        await call.message.answer("🔍 Введи часть имени (или /cancel):")
    await call.answer()


def _match(people: list, query: str) -> list:
    q = query.lower()
    return [p for p in people if q in p.full_name.lower()]


@router.message(EntitySearch.query, F.text)
async def on_search_query(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    data = await state.get_data()
    await state.clear()
    if not text or text.startswith("/"):
        await message.answer("Поиск отменён.")
        return
    kind = data.get("search_kind")
    ctx = data.get("search_ctx") or []
    tg_id = message.from_user.id if message.from_user else 0

    if kind == "pool":
        async with session_scope() as s:
            teacher = await admins_svc.get_by_tg_id(s, tg_id)
            if teacher is None:
                return
            people = _match(await persons_svc.admin_visible_persons(s, teacher.id), text)
            levels = await persons_svc.admin_pool_levels(s, teacher.id)
        if not people:
            await message.answer(f"По «{text}» никого. Открой 🆘 Саппорты заново.")
            return
        await message.answer(
            f"🔍 «{text}» — {len(people)}:", reply_markup=pool_keyboard(people, levels)
        )
        return

    if kind == "walkin":
        survey_id, group_id = int(ctx[0]), int(ctx[1])
        async with session_scope() as s:
            group = await accessible_group(s, group_id, tg_id)
            if group is None:
                return
            roster = await surveys_svc.attendance_roster(s, survey_id)
            roster_ids = {p.id for p, _ in roster}
            all_persons = await persons_svc.list_persons(s, group.school_id)
            people = _match([p for p in all_persons if p.id not in roster_ids], text)
        if not people:
            await message.answer(f"По «{text}» никого среди ещё не отмеченных.")
            return
        await message.answer(
            f"🔍 «{text}» — {len(people)}:",
            reply_markup=walk_in_picker_keyboard(survey_id, group_id, people),
        )
        return

    if kind == "students":
        group_id = int(ctx[0])
        async with session_scope() as s:
            group = await accessible_group(s, group_id, tg_id)
            if group is None:
                return
            people = _match(await persons_svc.group_members(s, group_id), text)
        if not people:
            await message.answer(f"По «{text}» в группе никого.")
            return
        await message.answer(
            f"🔍 «{text}» — {len(people)}:",
            reply_markup=students_keyboard(group_id, people),
        )
