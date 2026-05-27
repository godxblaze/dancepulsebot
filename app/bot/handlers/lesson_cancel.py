"""Отмена одного занятия: 3-шаговый flow (pick → confirm → reason → execute).

Извлечено из admin.py в рамках C1. Использует `_access.accessible_group`.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers._access import accessible_group
from app.bot.keyboards import (
    MENU_TEXTS,
    confirm_cancel_lesson_keyboard,
    pick_lesson_to_cancel_keyboard,
    skip_keyboard,
)
from app.bot.states import CancelLesson
from app.bot.utils import format_group_title
from app.db.models import Answer, Person, SupportCallStatus, SurveyStatus
from app.db.session import session_scope
from app.services import cancellations as cancellations_svc
from app.services import schools as schools_svc
from app.services import supports as supports_svc
from app.services import surveys as surveys_svc
from app.services.schedule import format_lesson_at, upcoming_lessons, utc_now

log = logging.getLogger(__name__)
router = Router()


@router.callback_query(F.data.startswith("grpcancel:"))
async def cb_lesson_cancel_pick(call: CallbackQuery) -> None:
    """Шаг 1: показываем 5 ближайших занятий — preпод выбирает какое отменить."""
    group_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        group = await accessible_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        upcoming = upcoming_lessons(group.weekdays, group.lesson_time, utc_now(), n=5)
        if not upcoming:
            await call.answer("Нет ближайших занятий", show_alert=True)
            return
        items: list[tuple[int, str, bool]] = []
        for dt in upcoming:
            cancelled = await cancellations_svc.is_cancelled(s, group.id, dt)
            label = format_lesson_at(dt, "%d.%m (%a) в %H:%M")
            items.append((int(dt.timestamp()), label, cancelled))
        title = format_group_title(group)
    await call.message.edit_text(
        f"Отменить какое занятие «{title}»?",
        reply_markup=pick_lesson_to_cancel_keyboard(group_id, items),
    )
    await call.answer()


@router.callback_query(F.data.startswith("grpcanceldt:"))
async def cb_lesson_cancel_confirm(call: CallbackQuery) -> None:
    """Шаг 2: confirmation выбранной даты."""
    parts = (call.data or "").split(":")
    group_id, ts = int(parts[1]), int(parts[2])
    lesson_at = datetime.fromtimestamp(ts, UTC)
    async with session_scope() as s:
        group = await accessible_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        if await cancellations_svc.is_cancelled(s, group.id, lesson_at):
            await call.answer("Это занятие уже отменено", show_alert=True)
            return
        title = format_group_title(group)
    await call.message.edit_text(
        f"Отменить занятие «{title}» {format_lesson_at(lesson_at)}?\n\n"
        f"Бот не разошлёт опрос; всем кто уже ответил «приду» и саппортам "
        f"уйдёт уведомление об отмене.",
        reply_markup=confirm_cancel_lesson_keyboard(group_id, ts),
    )
    await call.answer()


@router.callback_query(F.data.startswith("grpcancelok:"))
async def cb_lesson_cancel_ask_reason(call: CallbackQuery, state: FSMContext) -> None:
    """Шаг 3a: подтверждение получено — спрашиваем опциональную причину."""
    parts = (call.data or "").split(":")
    group_id, ts = int(parts[1]), int(parts[2])
    await state.set_state(CancelLesson.reason)
    await state.update_data(group_id=group_id, ts=ts)
    if call.message:
        await call.message.edit_text(
            "Хочешь приложить причину отмены? Напиши одной строкой — "
            "она пойдёт в уведомление ученикам. Или «Пропустить».",
            reply_markup=skip_keyboard("cnclreason:skip"),
        )
    await call.answer()


@router.message(Command("cancel"), CancelLesson.reason)
async def lesson_cancel_reason_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Окей, отмену занятия отменил — ничего не изменилось.")


@router.callback_query(CancelLesson.reason, F.data == "cnclreason:skip")
async def cb_lesson_cancel_skip_reason(
    call: CallbackQuery, state: FSMContext, bot: Bot
) -> None:
    data = await state.get_data()
    await state.clear()
    result = await _do_cancel_lesson(
        call.from_user.id, int(data["group_id"]), int(data["ts"]), None, bot
    )
    if call.message:
        await call.message.edit_text(result)
    await call.answer()


@router.message(CancelLesson.reason, F.text, ~F.text.in_(MENU_TEXTS))
async def lesson_cancel_reason_typed(
    message: Message, state: FSMContext, bot: Bot
) -> None:
    data = await state.get_data()
    reason = (message.text or "").strip() or None
    await state.clear()
    result = await _do_cancel_lesson(
        message.from_user.id, int(data["group_id"]), int(data["ts"]), reason, bot
    )
    await message.answer(result)


async def _do_cancel_lesson(
    initiator_tg_id: int,
    group_id: int,
    ts: int,
    reason: str | None,
    bot: Bot,
) -> str:
    """Пишет cancellation, закрывает survey/support, шлёт уведомления.
    Возвращает текст итоговой сводки для отправителя."""
    lesson_at = datetime.fromtimestamp(ts, UTC)
    yes_tg_ids: list[int] = []
    support_tg_ids: list[int] = []
    coadmin_tg_ids: list[int] = []
    title = ""
    async with session_scope() as s:
        group = await accessible_group(s, group_id, initiator_tg_id)
        if group is None:
            return "Группа не найдена."
        title = format_group_title(group)
        await cancellations_svc.cancel(s, group.id, lesson_at, reason=reason)
        if group.school_id is not None:
            all_tgs = await schools_svc.member_tg_ids(s, group.school_id)
            coadmin_tg_ids = [tg for tg in all_tgs if tg != initiator_tg_id]
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

    reason_suffix = f"\nПричина: {reason}" if reason else ""
    notice = f"⚠️ Занятие «{title}» {format_lesson_at(lesson_at)} отменено.{reason_suffix}"
    for tg in yes_tg_ids:
        try:
            await bot.send_message(tg, notice)
        except Exception as e:  # noqa: BLE001
            log.info("cancel-notice to %s failed: %s", tg, e)
    sup_notice = (
        f"⚠️ Помощь на занятии «{title}» {format_lesson_at(lesson_at)} "
        f"больше не нужна — занятие отменено.{reason_suffix}"
    )
    for tg in support_tg_ids:
        try:
            await bot.send_message(tg, sup_notice)
        except Exception as e:  # noqa: BLE001
            log.info("cancel-notice (support) to %s failed: %s", tg, e)

    coadmin_notice = (
        f"ℹ️ Занятие «{title}» {format_lesson_at(lesson_at)} отменено "
        f"(отменил другой админ школы).{reason_suffix}"
    )
    for tg in coadmin_tg_ids:
        try:
            await bot.send_message(tg, coadmin_notice)
        except Exception as e:  # noqa: BLE001
            log.info("cancel-notice (coadmin) to %s failed: %s", tg, e)

    return (
        f"🚫 Занятие «{title}» {format_lesson_at(lesson_at)} отменено. "
        f"Уведомления отправлены ({len(yes_tg_ids)} ученикам, "
        f"{len(support_tg_ids)} саппортам, {len(coadmin_tg_ids)} админам школы)."
    )
