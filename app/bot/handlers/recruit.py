from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from app.bot.keyboards import support_ask_keyboard, support_pick_keyboard
from app.db.models import Person, SupportCallStatus, Teacher
from app.db.session import session_scope
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import supports as supports_svc
from app.services import surveys as surveys_svc

log = logging.getLogger(__name__)
router = Router()


def _selected_ids(markup: InlineKeyboardMarkup | None) -> set[int]:
    """Person ids currently ticked (✅) in a support-pick keyboard."""
    selected: set[int] = set()
    if markup is None:
        return selected
    for row in markup.inline_keyboard:
        for btn in row:
            data = btn.callback_data or ""
            if data.startswith("pick:") and btn.text.startswith("✅"):
                selected.add(int(data.split(":")[2]))
    return selected


async def _call_context(session, call_id: int):
    """Returns (call, survey, group) for a SupportCall — any may be None."""
    call = await supports_svc.get_call(session, call_id)
    if call is None:
        return None, None, None
    survey = await surveys_svc.get_survey(session, call.survey_id)
    group = await groups_svc.get_group(session, survey.group_id) if survey else None
    return call, survey, group


@router.callback_query(F.data.startswith("pick:"))
async def cb_support_pick(call: CallbackQuery) -> None:
    """Teacher ticks/unticks a pool member in the recruit picker."""
    parts = (call.data or "").split(":")
    call_id, person_id = int(parts[1]), int(parts[2])
    selected = _selected_ids(call.message.reply_markup if call.message else None)
    selected ^= {person_id}  # toggle
    async with session_scope() as s:
        sc, _survey, group = await _call_context(s, call_id)
        if sc is None or group is None:
            await call.answer()
            return
        teacher = await s.get(Teacher, group.teacher_id)
        if teacher is None or teacher.tg_user_id != call.from_user.id:
            await call.answer()
            return
        candidates = await persons_svc.support_candidates(
            s, group.teacher_id, sc.gender, group.id
        )
    if call.message:
        await call.message.edit_reply_markup(
            reply_markup=support_pick_keyboard(call_id, candidates, selected)
        )
    await call.answer()


@router.callback_query(F.data.startswith("picksend:"))
async def cb_support_pick_send(call: CallbackQuery, bot: Bot) -> None:
    """Teacher sends the recruit invites to the ticked pool members."""
    call_id = int((call.data or "").split(":")[1])
    selected = _selected_ids(call.message.reply_markup if call.message else None)
    if not selected:
        await call.answer("Сначала отметь, кого позвать", show_alert=True)
        return
    async with session_scope() as s:
        sc, survey, group = await _call_context(s, call_id)
        if sc is None or survey is None or group is None:
            await call.answer("Уже обработано", show_alert=True)
            return
        teacher = await s.get(Teacher, group.teacher_id)
        if teacher is None or teacher.tg_user_id != call.from_user.id:
            await call.answer()
            return
        if sc.status == SupportCallStatus.closed:
            await call.answer("Набор уже закрыт", show_alert=True)
            return
        invited: list[tuple[int, str]] = []
        for pid in selected:
            p = await s.get(Person, pid)
            if p is not None:
                invited.append((p.tg_user_id, p.full_name))
        if not invited:
            await call.answer("Никого не нашлось", show_alert=True)
            return
        await supports_svc.set_status(s, call_id, SupportCallStatus.open)
        group_name = group.name
        lesson_time = group.lesson_time
        lesson_at = survey.lesson_at

    text = (
        f"🆘 Нужна помощь!\n"
        f"Группа «{group_name}», занятие {lesson_at:%d.%m} в {lesson_time}.\n"
        f"Не хватает пары — сможешь подойти выручить?"
    )
    for tg_id, _name in invited:
        try:
            await bot.send_message(
                tg_id, text, reply_markup=support_ask_keyboard(call_id)
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Could not invite support %s: %s", tg_id, e)
    if call.message:
        names = ", ".join(n for _, n in invited)
        await call.message.edit_text(
            f"✅ Позвал: {names}.\nНапишу, как откликнутся."
        )
    await call.answer()


@router.callback_query(F.data.startswith("sup:join:"))
async def cb_support_join(call: CallbackQuery, bot: Bot) -> None:
    call_id = int((call.data or "").split(":")[2])
    async with session_scope() as s:
        sc = await supports_svc.get_call(s, call_id)
        if sc is None:
            await call.answer()
            return
        survey = await surveys_svc.get_survey(s, sc.survey_id)
        group = await groups_svc.get_group(s, survey.group_id) if survey else None
        if group is None:
            await call.answer()
            return
        person = await persons_svc.get_person(s, group.teacher_id, call.from_user.id)
        if person is None:
            await call.answer()
            return
        accepted, filled, needed = await supports_svc.claim(s, call_id, person.id)
        teacher = await s.get(Teacher, group.teacher_id)
        teacher_tg = teacher.tg_user_id if teacher else None
        person_name = person.full_name
        group_name = group.name

    if not accepted:
        if call.message:
            await call.message.edit_text(
                "Уже набрали нужное число — спасибо, что откликнулся!"
            )
        await call.answer()
        return

    if call.message:
        await call.message.edit_text(
            "✅ Спасибо! Ты записан саппортом — ждём тебя на занятии."
        )
    await call.answer("Записал!")
    if teacher_tg is not None:
        done = "  Набор закрыт ✅" if filled >= needed else ""
        try:
            await bot.send_message(
                teacher_tg,
                f"🆘 {person_name} придёт выручить «{group_name}» — "
                f"{filled}/{needed}.{done}",
            )
        except Exception:  # noqa: BLE001
            pass


@router.callback_query(F.data.startswith("sup:skip:"))
async def cb_support_skip(call: CallbackQuery) -> None:
    if call.message:
        await call.message.edit_text("Ок, в этот раз без тебя. Спасибо!")
    await call.answer()
