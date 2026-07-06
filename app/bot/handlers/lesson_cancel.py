"""Отмена одного занятия: pick → confirm-with-reason-presets → execute.

Каждый пресет причины на confirm-экране исполняет отмену НЕМЕДЛЕННО — брошенный
флоу не может отменить занятие, а зависший FSM-state не может выполнить отмену
задним числом (единственный путь в FSM — «Другое», и он протухает через
REASON_STALE_AFTER). Извлечено из admin.py в рамках C1. Использует
`_access.accessible_group`.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import ui
from app.bot.handlers._access import accessible_group
from app.bot.keyboards import (
    MENU_TEXTS,
    confirm_cancel_lesson_keyboard,
    confirm_typed_cancel_reason_keyboard,
    pick_lesson_to_cancel_keyboard,
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

# Пресеты причины (грpcancelreason:<gid>:<ts>:<preset>) — None = без причины.
_REASON_PRESETS: dict[str, str | None] = {
    "sick": "Болезнь преподавателя",
    "venue": "Зал недоступен",
    "none": None,
}
# «Другое» ведёт в текстовый FSM-шаг; если он не завершён за это время —
# протухает (state.clear() вместо исполнения отмены по случайному тексту).
REASON_STALE_AFTER = timedelta(minutes=10)


@router.callback_query(F.data.startswith("grpcancel:"))
async def cb_lesson_cancel_pick(call: CallbackQuery) -> None:
    """Шаг 1: показываем 5 ближайших занятий — preпод выбирает какое отменить."""
    group_id = int(call.data.split(":", 1)[1])
    async with session_scope() as s:
        group = await accessible_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        tz_name = (
            await schools_svc.get_timezone(s, group.school_id)
            if group.school_id else None
        )
        upcoming = upcoming_lessons(
            group.weekdays, group.lesson_time, utc_now(), n=5, tz=tz_name,
        )
        if not upcoming:
            await call.answer("Нет ближайших занятий", show_alert=True)
            return
        items: list[tuple[int, str, bool]] = []
        for dt in upcoming:
            cancelled = await cancellations_svc.is_cancelled(s, group.id, dt)
            label = format_lesson_at(dt, "%d.%m (%a) в %H:%M", tz=tz_name)
            items.append((int(dt.timestamp()), label, cancelled))
        title = format_group_title(group)
    if call.message:
        await call.message.edit_text(
            f"Отменить какое занятие «{title}»?",
            reply_markup=pick_lesson_to_cancel_keyboard(group_id, items),
        )
    await call.answer()


@router.callback_query(F.data.startswith("grpcanceldt:"))
async def cb_lesson_cancel_confirm(call: CallbackQuery) -> None:
    """Шаг 2: confirm-экран с пресетами причины — каждый тап исполняет отмену
    немедленно (шаг 13 аудита: брошенный флоу больше не может «почти отменить»)."""
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
        tz_name = (
            await schools_svc.get_timezone(s, group.school_id)
            if group.school_id else None
        )
    if call.message:
        await call.message.edit_text(
            f"Отменить занятие «{title}» {format_lesson_at(lesson_at, tz=tz_name)}?\n\n"
            f"Бот не разошлёт опрос; всем кто уже ответил «приду» и саппортам "
            f"уйдёт уведомление об отмене.\n\n"
            f"Выбери причину — отмена исполнится сразу по тапу:",
            reply_markup=confirm_cancel_lesson_keyboard(group_id, ts),
        )
    await call.answer()


@router.callback_query(F.data.startswith("grpcancelreason:"))
async def cb_lesson_cancel_execute(call: CallbackQuery, bot: Bot) -> None:
    """Тап пресета причины — отмена исполняется немедленно, без FSM."""
    parts = (call.data or "").split(":")
    group_id, ts, preset = int(parts[1]), int(parts[2]), parts[3]
    reason = _REASON_PRESETS.get(preset)
    result = await _do_cancel_lesson(call.from_user.id, group_id, ts, reason, bot)
    if call.message:
        await call.message.edit_text(result)
    await call.answer()


@router.callback_query(F.data.startswith("grpcancelother:"))
async def cb_lesson_cancel_other(call: CallbackQuery, state: FSMContext) -> None:
    """«Другое» — единственный путь в текстовый шаг причины; asked_at делает
    его протухающим (см. lesson_cancel_reason_typed)."""
    parts = (call.data or "").split(":")
    group_id, ts = int(parts[1]), int(parts[2])
    await state.set_state(CancelLesson.reason)
    await state.update_data(group_id=group_id, ts=ts, asked_at=utc_now().timestamp())
    if call.message:
        await call.message.edit_text(
            "Напиши причину отмены одной строкой — она пойдёт в уведомление "
            "ученикам."
        )
    await call.answer()


@router.message(Command("cancel"), CancelLesson.reason)
async def lesson_cancel_reason_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Окей, отмену занятия отменил — ничего не изменилось.")


@router.message(CancelLesson.reason, F.text, ~F.text.in_(MENU_TEXTS))
async def lesson_cancel_reason_typed(
    message: Message, state: FSMContext
) -> None:
    """Не исполняем отмену сразу по любому тексту — даже в пределах
    REASON_STALE_AFTER это может оказаться не причиной отмены, а случайным
    сообщением не по теме. Просим явное подтверждение с показом причины."""
    data = await state.get_data()
    asked_at = data.get("asked_at")
    if asked_at is None or utc_now().timestamp() - asked_at > REASON_STALE_AFTER.total_seconds():
        # Флоу протух (>10 мин без ответа) — не исполняем отмену по случайному
        # тексту, введённому куда-то в другой момент.
        await state.clear()
        await message.answer("Флоу отмены устарел — начни заново через карточку группы.")
        return
    reason = (message.text or "").strip() or None
    group_id, ts = int(data["group_id"]), int(data["ts"])
    await state.set_state(CancelLesson.confirm_reason)
    await state.update_data(reason=reason)
    reason_line = f"«{reason}»" if reason else "без причины"
    await message.answer(
        f"Отменить занятие с причиной {reason_line}?",
        reply_markup=confirm_typed_cancel_reason_keyboard(group_id, ts),
    )


@router.callback_query(CancelLesson.confirm_reason, F.data.startswith("grpcancelreasonok:"))
async def lesson_cancel_reason_confirm(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Подтверждение введённой текстом причины — реальное исполнение отмены."""
    data = await state.get_data()
    await state.clear()
    parts = (call.data or "").split(":")
    group_id, ts = int(parts[1]), int(parts[2])
    # group_id/ts из callback_data должны совпасть с тем, что уже в FSM —
    # защита от «подтверждения» чужого/устаревшего флоу.
    if group_id != int(data.get("group_id", -1)) or ts != int(data.get("ts", -1)):
        await call.answer("Флоу отмены устарел — начни заново.", show_alert=True)
        return
    result = await _do_cancel_lesson(
        call.from_user.id, group_id, ts, data.get("reason"), bot
    )
    if call.message:
        await call.message.edit_text(result)
    await call.answer()


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
    tz_name: str | None = None
    async with session_scope() as s:
        group = await accessible_group(s, group_id, initiator_tg_id)
        if group is None:
            return "Группа не найдена."
        title = format_group_title(group)
        tz_name = (
            await schools_svc.get_timezone(s, group.school_id)
            if group.school_id else None
        )
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
            if sc is not None:
                # Независимо от call.status — в т.ч. closed по filled, ровно
                # успешный сценарий киллер-фичи: заявившиеся саппорты должны
                # узнать об отмене так же, как «приду»-ученики.
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
    when_str = format_lesson_at(lesson_at, tz=tz_name)
    notice = f"⚠️ Занятие «{title}» {when_str} отменено.{reason_suffix}"
    for tg in yes_tg_ids:
        try:
            await bot.send_message(tg, notice)
        except Exception as e:  # noqa: BLE001
            log.info("cancel-notice to %s failed: %s", tg, e)
    sup_notice = (
        f"⚠️ Помощь на занятии «{title}» {when_str} "
        f"больше не нужна — занятие отменено.{reason_suffix}"
    )
    for tg in support_tg_ids:
        try:
            await bot.send_message(tg, sup_notice)
        except Exception as e:  # noqa: BLE001
            log.info("cancel-notice (support) to %s failed: %s", tg, e)

    coadmin_notice = (
        f"ℹ️ Занятие «{title}» {when_str} отменено "
        f"(отменил другой админ школы).{reason_suffix}"
    )
    for tg in coadmin_tg_ids:
        try:
            await bot.send_message(tg, coadmin_notice)
        except Exception as e:  # noqa: BLE001
            log.info("cancel-notice (coadmin) to %s failed: %s", tg, e)

    return (
        f"🚫 Занятие «{title}» {when_str} отменено. "
        "Уведомления отправлены ("
        f"{ui.plural_with_count(len(yes_tg_ids), 'ученику', 'ученикам', 'ученикам')}, "
        f"{ui.plural_with_count(len(support_tg_ids), 'саппорту', 'саппортам', 'саппортам')}, "
        f"{ui.plural_with_count(len(coadmin_tg_ids), 'админу', 'админам', 'админам')} школы)."
    )
