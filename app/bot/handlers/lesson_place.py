"""Место/зал одного конкретного занятия: pick → текст одной строкой → готово.

Для школ, где группа каждый раз в разных залах. Override живёт на дате занятия
([LessonPlaceOverride](../../db/models.py)) и подставляется во все сообщения
этого занятия (опрос, напоминания, реактивация, инвайты саппортам) через
scheduler.lesson_place. При установке уже ответившим «приду» и заявившимся
саппортам уходит уведомление. Структура зеркалит lesson_cancel.py.

Осознанное отклонение от конвенции per-call asyncio.Lock tap-хендлеров добора:
здесь lock не нужен — жмёт одиночный админ, запись это идемпотентный upsert по
unique(group_id, lesson_at), худший исход гонки — IntegrityError с откатом
одной из транзакций, не порча данных.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot.handlers._access import accessible_group
from app.bot.handlers.lesson_cancel import REASON_STALE_AFTER
from app.bot.keyboards import MENU_TEXTS, pick_lesson_place_keyboard
from app.bot.states import SetLessonPlace
from app.bot.utils import format_group_title, send_with_retry
from app.db.models import Answer, Person, Response, SupportClaim
from app.db.session import session_scope
from app.services import cancellations as cancellations_svc
from app.services import place_overrides as overrides_svc
from app.services import schools as schools_svc
from app.services import supports as supports_svc
from app.services import surveys as surveys_svc
from app.services.schedule import format_lesson_at, upcoming_lessons, utc_now

log = logging.getLogger(__name__)
router = Router()


@router.callback_query(F.data.startswith("grpplace:"))
async def cb_lesson_place_pick(call: CallbackQuery) -> None:
    """Шаг 1: ближайшие занятия (кроме отменённых) — на кнопках видно, где
    место уже переопределено."""
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
        items: list[tuple[int, str, str | None]] = []
        for dt in upcoming:
            if await cancellations_svc.is_cancelled(s, group.id, dt):
                continue
            label = format_lesson_at(dt, "%d.%m (%a) в %H:%M", tz=tz_name)
            override = await overrides_svc.get_place(s, group.id, dt)
            items.append((int(dt.timestamp()), label, override))
        title = format_group_title(group)
    if not items:
        await call.answer("Нет ближайших занятий", show_alert=True)
        return
    if call.message:
        await call.message.edit_text(
            f"На каком занятии «{title}» другое место/зал?",
            reply_markup=pick_lesson_place_keyboard(group_id, items),
        )
    await call.answer()


@router.callback_query(F.data.startswith("grpplacedt:"))
async def cb_lesson_place_ask(call: CallbackQuery, state: FSMContext) -> None:
    """Шаг 2: текстовый ввод места. Протухает как у отмены (см.
    lesson_place_typed) — случайный текст спустя время ничего не перезапишет."""
    parts = (call.data or "").split(":")
    group_id, ts = int(parts[1]), int(parts[2])
    lesson_at = datetime.fromtimestamp(ts, UTC)
    async with session_scope() as s:
        group = await accessible_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        tz_name = (
            await schools_svc.get_timezone(s, group.school_id)
            if group.school_id else None
        )
        current = await overrides_svc.get_place(s, group.id, lesson_at)
    await state.set_state(SetLessonPlace.place)
    await state.update_data(group_id=group_id, ts=ts, asked_at=utc_now().timestamp())
    when = format_lesson_at(lesson_at, tz=tz_name)
    lines = [
        f"Где пройдёт занятие {when}? Напиши место/зал одной строкой "
        f"(например: «зал 5» или «Тверская 10, зал 2»).",
    ]
    if current:
        lines.append(f"Сейчас задано: «{current}».")
    rows = []
    if current:
        rows.append([InlineKeyboardButton(
            text="🗑 Сбросить (место группы)",
            callback_data=f"grpplaceclear:{group_id}:{ts}",
        )])
    rows.append([InlineKeyboardButton(
        text="◀ Назад", callback_data=f"grpplaceback:{group_id}",
    )])
    if call.message:
        await call.message.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    await call.answer()


@router.callback_query(F.data.startswith("grpplaceback:"))
async def cb_lesson_place_back(call: CallbackQuery, state: FSMContext) -> None:
    """«◀ Назад» с текстового шага — сбрасываем state и возвращаем к списку
    занятий (cb_lesson_place_pick не смотрит на префикс callback_data)."""
    await state.clear()
    await cb_lesson_place_pick(call)


@router.callback_query(F.data.startswith("grpplaceclear:"))
async def cb_lesson_place_clear(call: CallbackQuery, state: FSMContext) -> None:
    parts = (call.data or "").split(":")
    group_id, ts = int(parts[1]), int(parts[2])
    lesson_at = datetime.fromtimestamp(ts, UTC)
    async with session_scope() as s:
        group = await accessible_group(s, group_id, call.from_user.id)
        if group is None:
            await call.answer("Группа не найдена", show_alert=True)
            return
        cleared = await overrides_svc.clear(s, group.id, lesson_at)
    await state.clear()
    if call.message:
        await call.message.edit_text(
            "📍 Сброшено — действует обычное место группы."
            if cleared else "Тут и так действует обычное место группы."
        )
    await call.answer()


@router.message(Command("cancel"), SetLessonPlace.place)
async def lesson_place_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Окей, место занятия не менял.")


@router.message(SetLessonPlace.place, F.text, ~F.text.in_(MENU_TEXTS))
async def lesson_place_typed(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    asked_at = data.get("asked_at")
    if asked_at is None or utc_now().timestamp() - asked_at > REASON_STALE_AFTER.total_seconds():
        await state.clear()
        await message.answer("Флоу устарел — начни заново через карточку группы.")
        return
    place = (message.text or "").strip()[:120]
    if not place:
        await message.answer("Пустое место — напиши ещё раз или /cancel.")
        return
    group_id, ts = int(data["group_id"]), int(data["ts"])
    await state.clear()
    result = await _do_set_place(message.from_user.id, group_id, ts, place, bot)
    await message.answer(result)


async def _do_set_place(
    initiator_tg_id: int, group_id: int, ts: int, place: str, bot: Bot
) -> str:
    """Пишет override и уведомляет тех, кто уже собирался прийти («приду» +
    заявившиеся саппорты). Молчащие увидят новое место в напоминаниях 4ч/1ч,
    ещё не разосланный опрос возьмёт его автоматически."""
    lesson_at = datetime.fromtimestamp(ts, UTC)
    yes_tg_ids: list[int] = []
    support_tg_ids: list[int] = []
    async with session_scope() as s:
        group = await accessible_group(s, group_id, initiator_tg_id)
        if group is None:
            return "Группа не найдена."
        title = format_group_title(group)
        tz_name = (
            await schools_svc.get_timezone(s, group.school_id)
            if group.school_id else None
        )
        await overrides_svc.set_place(s, group.id, lesson_at, place)
        survey = await surveys_svc.find_attendance_survey(s, group.id, lesson_at)
        if survey is not None:
            from sqlalchemy import select as _sel
            rows = (await s.execute(
                _sel(Person.tg_user_id)
                .join(Response, Response.person_id == Person.id)
                .where(Response.survey_id == survey.id, Response.answer == Answer.yes)
            )).scalars().all()
            yes_tg_ids = list(rows)
            sc = await supports_svc.get_call_for_survey(s, survey.id)
            if sc is not None:
                sup_rows = (await s.execute(
                    _sel(Person.tg_user_id)
                    .join(SupportClaim, SupportClaim.person_id == Person.id)
                    .where(SupportClaim.support_call_id == sc.id)
                )).scalars().all()
                support_tg_ids = list(sup_rows)

    when = format_lesson_at(lesson_at, tz=tz_name)
    notice = f"📍 Занятие «{title}» {when} пройдёт: {place}"
    for tg in set(yes_tg_ids) | set(support_tg_ids):
        try:
            await send_with_retry(bot, tg, notice)
        except Exception as e:  # noqa: BLE001
            log.info("place-notice to %s failed: %s", tg, e)

    head = f"📍 Записал: «{title}» {when} — {place}."
    notified = len(set(yes_tg_ids) | set(support_tg_ids))
    if notified:
        return (
            f"{head} Уведомил уже собравшихся ({notified}); "
            f"остальные увидят место в опросе и напоминаниях."
        )
    return f"{head} Место попадёт в опрос и напоминания автоматически."
