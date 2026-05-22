from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from app.bot.keyboards import support_ask_keyboard
from app.db.models import SupportCall, SupportCallStatus, SupportScope, Teacher
from app.db.session import session_scope
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import supports as supports_svc
from app.services import surveys as surveys_svc

log = logging.getLogger(__name__)
router = Router()


async def _owner_tg(session, sc: SupportCall) -> int | None:
    """Telegram id of the teacher who owns the lesson this call belongs to."""
    survey = await surveys_svc.get_survey(session, sc.survey_id)
    group = await groups_svc.get_group(session, survey.group_id) if survey else None
    if group is None:
        return None
    teacher = await session.get(Teacher, group.teacher_id)
    return teacher.tg_user_id if teacher else None


async def broadcast_support_call(bot: Bot, call_id: int) -> None:
    """Message the support pools asking them to cover a lesson."""
    async with session_scope() as s:
        call = await supports_svc.get_call(s, call_id)
        if call is None or call.status != SupportCallStatus.open:
            return
        survey = await surveys_svc.get_survey(s, call.survey_id)
        group = await groups_svc.get_group(s, survey.group_id) if survey else None
        if survey is None or group is None:
            return
        teacher = await s.get(Teacher, group.teacher_id)
        include_open = (
            teacher is not None and teacher.support_scope == SupportScope.own_and_open
        )
        candidates = await persons_svc.support_candidates(
            s, group.teacher_id, call.gender, group.id, include_open
        )
        recipients = [p.tg_user_id for p in candidates]
        group_name = group.name
        lesson_time = group.lesson_time
        lesson_at = survey.lesson_at
        teacher_tg = teacher.tg_user_id if teacher else None

    if not recipients:
        if teacher_tg is not None:
            try:
                await bot.send_message(
                    teacher_tg,
                    "В пуле нет подходящих саппортов на это занятие — позови вручную.",
                )
            except Exception:  # noqa: BLE001
                pass
        return

    text = (
        f"🆘 Нужна помощь!\n"
        f"Группа «{group_name}», занятие {lesson_at:%d.%m} в {lesson_time}.\n"
        f"Не хватает пары — сможешь подойти выручить?"
    )
    for tg_id in recipients:
        try:
            await bot.send_message(
                tg_id, text, reply_markup=support_ask_keyboard(call_id)
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Could not ask support %s: %s", tg_id, e)


@router.callback_query(F.data.startswith("sup:yes:"))
async def cb_support_confirm_yes(call: CallbackQuery, bot: Bot) -> None:
    call_id = int((call.data or "").split(":")[2])
    async with session_scope() as s:
        sc = await supports_svc.get_call(s, call_id)
        if sc is None or sc.status != SupportCallStatus.pending:
            await call.answer("Уже обработано", show_alert=True)
            return
        if await _owner_tg(s, sc) != call.from_user.id:
            await call.answer()
            return
        await supports_svc.set_status(s, call_id, SupportCallStatus.open)
    if call.message:
        await call.message.edit_text(
            "✅ Запускаю сбор саппортов — напишу, как будут отклики."
        )
    await call.answer()
    await broadcast_support_call(bot, call_id)


@router.callback_query(F.data.startswith("sup:no:"))
async def cb_support_confirm_no(call: CallbackQuery) -> None:
    call_id = int((call.data or "").split(":")[2])
    async with session_scope() as s:
        sc = await supports_svc.get_call(s, call_id)
        if sc is None or sc.status != SupportCallStatus.pending:
            await call.answer()
            return
        if await _owner_tg(s, sc) != call.from_user.id:
            await call.answer()
            return
        await supports_svc.set_status(s, call_id, SupportCallStatus.closed)
    if call.message:
        await call.message.edit_text("Ок, саппортов не зовём.")
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
