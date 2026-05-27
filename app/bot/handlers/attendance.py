"""Post-lesson отметка посещаемости + walk-in. Извлечено из admin.py в C1."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.handlers._access import accessible_group
from app.bot.keyboards import (
    attendance_mark_keyboard,
    attended_from_code,
    closed_surveys_keyboard,
    walk_in_picker_keyboard,
)
from app.bot.utils import format_group_title
from app.db.models import Person
from app.db.session import session_scope
from app.services import persons as persons_svc
from app.services import surveys as surveys_svc

log = logging.getLogger(__name__)
router = Router()


@router.callback_query(F.data.startswith("grpattnmark:"))
async def cb_attendance_pick_lesson(call: CallbackQuery) -> None:
    """Показываем 5 последних закрытых занятий — preпод выбирает какое отмечать."""
    group_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        group = await accessible_group(s, group_id, call.from_user.id)
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
    call: CallbackQuery, survey_id: int, group_id: int, page: int = 0,
) -> None:
    async with session_scope() as s:
        group = await accessible_group(s, group_id, call.from_user.id)
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
        head,
        reply_markup=attendance_mark_keyboard(survey_id, group_id, items, page=page),
    )


@router.callback_query(F.data.startswith("atnpg:"))
async def cb_attendance_page(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    survey_id, group_id, page = int(parts[1]), int(parts[2]), int(parts[3])
    await _render_attendance_roster(call, survey_id, group_id, page=page)
    await call.answer()


@router.callback_query(F.data.startswith("atnlesson:"))
async def cb_attendance_open_lesson(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    survey_id, group_id = int(parts[1]), int(parts[2])
    await _render_attendance_roster(call, survey_id, group_id)
    await call.answer()


async def _render_walk_in_picker(
    call: CallbackQuery, survey_id: int, group_id: int, page: int = 0,
) -> None:
    async with session_scope() as s:
        group = await accessible_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        survey = await surveys_svc.get_survey(s, survey_id)
        if survey is None or survey.group_id != group_id:
            await call.answer("Опрос не найден", show_alert=True)
            return
        roster = await surveys_svc.attendance_roster(s, survey_id)
        roster_ids = {p.id for p, _ in roster}
        all_persons = await persons_svc.list_persons(s, group.school_id)
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
        reply_markup=walk_in_picker_keyboard(survey_id, group_id, candidates, page=page),
    )


@router.callback_query(F.data.startswith("atnwk:"))
async def cb_walk_in_pick(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    survey_id, group_id = int(parts[1]), int(parts[2])
    await _render_walk_in_picker(call, survey_id, group_id, page=0)
    await call.answer()


@router.callback_query(F.data.startswith("wkpg:"))
async def cb_walk_in_page(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    survey_id, group_id, page = int(parts[1]), int(parts[2]), int(parts[3])
    await _render_walk_in_picker(call, survey_id, group_id, page=page)
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
        group = await accessible_group(s, survey.group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        person = await s.get(Person, person_id)
        if person is None or person.school_id != group.school_id:
            await call.answer("Не найдено", show_alert=True)
            return
        # Walk-in — разовый гость, без Membership; постоянная запись —
        # отдельной кнопкой в карточке ученика (pcardadd).
        await surveys_svc.add_walk_in(s, survey_id, person_id)
        group_id = group.id
    await _render_attendance_roster(call, survey_id, group_id)
    await call.answer("Добавил как разового гостя")


@router.callback_query(F.data.startswith("atntog:"))
async def cb_attendance_toggle(call: CallbackQuery) -> None:
    """Переключение состояния: None → True → False → None. idempotency-guard
    через expected-state в callback_data — два быстрых тапа не перескочат.
    Опциональный 5-й сегмент — текущая страница ростера для возврата."""
    parts = (call.data or "").split(":")
    survey_id, person_id = int(parts[1]), int(parts[2])
    expected = attended_from_code(parts[3]) if len(parts) > 3 else None
    page = int(parts[4]) if len(parts) > 4 else 0
    async with session_scope() as s:
        survey = await surveys_svc.get_survey(s, survey_id)
        if survey is None:
            await call.answer("Опрос не найден", show_alert=True)
            return
        group = await accessible_group(s, survey.group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        from sqlalchemy import select as _sel

        from app.db.models import Response
        cur = (await s.execute(
            _sel(Response.actually_attended).where(
                Response.survey_id == survey_id, Response.person_id == person_id
            )
        )).scalar_one_or_none()
        stale = len(parts) > 3 and cur != expected
        if not stale:
            new_value = True if cur is None else (False if cur is True else None)
            await surveys_svc.set_actual_attendance(
                s, survey_id, person_id, new_value
            )
        group_id = group.id
    # Перерисовываем ростер даже при stale — иначе быстрый тап «зависает».
    await _render_attendance_roster(call, survey_id, group_id, page=page)
    await call.answer("Уже обновлено" if stale else "")
