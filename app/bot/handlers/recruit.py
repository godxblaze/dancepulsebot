from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from app.bot.keyboards import (
    support_ask_keyboard,
    support_flip_keyboard,
    support_pick_keyboard,
)
from app.bot.utils import format_group_title, format_place
from app.db.models import Admin, Person, SupportCallStatus
from app.db.session import session_scope
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import supports as supports_svc
from app.services import surveys as surveys_svc
from app.services import venues as venues_svc

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
        teacher = await s.get(Admin, group.admin_id)
        if teacher is None or teacher.tg_user_id != call.from_user.id:
            await call.answer()
            return
        candidates = await persons_svc.support_candidates(
            s, group.admin_id, sc.gender, group.id
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
        teacher = await s.get(Admin, group.admin_id)
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
        venue = (
            await venues_svc.get_venue(s, group.venue_id)
            if group.venue_id is not None
            else None
        )
        title = format_group_title(group)
        place = format_place(group, venue)
        teachers_note = group.teachers_note
        lesson_time = group.lesson_time
        lesson_at = survey.lesson_at

    text_lines = [
        "🆘 Нужна помощь!",
        f"«{title}» — {lesson_at:%d.%m} в {lesson_time}.",
    ]
    if place:
        text_lines.append(f"📍 {place}")
    if teachers_note:
        text_lines.append(f"👤 {teachers_note}")
    text_lines.append("Не хватает пары — сможешь подойти выручить?")
    text = "\n".join(text_lines)
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
        person = await persons_svc.get_person(s, group.admin_id, call.from_user.id)
        if person is None:
            await call.answer()
            return
        accepted, filled, needed = await supports_svc.claim(s, call_id, person.id)
        teacher = await s.get(Admin, group.admin_id)
        teacher_tg = teacher.tg_user_id if teacher else None
        person_name = person.full_name
        title = format_group_title(group)
        group_name = group.name
        lesson_at = survey.lesson_at

    when = f"{lesson_at:%d.%m в %H:%M}"
    if not accepted:
        # already-claimed branch covers the flip-to-yes case when the call is
        # full or the user is already on the list — same outcome either way.
        existing = await _is_already_claimed(call_id, person.id)
        if existing:
            if call.message:
                await call.message.edit_text(
                    f"✅ Ты записан саппортом на «{title}» {when} — "
                    f"ждём тебя на занятии.",
                    reply_markup=support_flip_keyboard(call_id, "join"),
                )
            await call.answer()
            return
        if call.message:
            await call.message.edit_text(
                f"Уже набрали нужное число на «{title}» {when} — "
                f"спасибо, что откликнулся!"
            )
        await call.answer()
        return

    if call.message:
        await call.message.edit_text(
            f"✅ Спасибо! Ты записан саппортом на «{title}» {when} — "
            f"ждём тебя на занятии.",
            reply_markup=support_flip_keyboard(call_id, "join"),
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
async def cb_support_skip(call: CallbackQuery, bot: Bot) -> None:
    call_id = int((call.data or "").split(":")[2])
    reverted_name: str | None = None
    filled = needed = 0
    teacher_tg: int | None = None
    title = "занятие"
    when = ""
    group_name = ""
    async with session_scope() as s:
        sc = await supports_svc.get_call(s, call_id)
        survey = await surveys_svc.get_survey(s, sc.survey_id) if sc else None
        group = (
            await groups_svc.get_group(s, survey.group_id) if survey else None
        )
        if group is not None and survey is not None:
            title = format_group_title(group)
            when = f"{survey.lesson_at:%d.%m в %H:%M}"
            group_name = group.name
            person = await persons_svc.get_person(
                s, group.admin_id, call.from_user.id
            )
            if person is not None and sc is not None:
                if await supports_svc.unclaim(s, call_id, person.id):
                    reverted_name = person.full_name
                    filled = await supports_svc.count_claims(s, call_id)
                    needed = sc.needed
                    teacher = await s.get(Admin, group.admin_id)
                    teacher_tg = teacher.tg_user_id if teacher else None

    if call.message:
        text = f"Понял, на «{title}» {when} не сможешь. Спасибо, что откликнулся!"
        await call.message.edit_text(
            text, reply_markup=support_flip_keyboard(call_id, "skip")
        )
    await call.answer()

    # If this was a flip-from-yes — tell the teacher the slot opened up again.
    if reverted_name is not None and teacher_tg is not None:
        try:
            await bot.send_message(
                teacher_tg,
                f"↩️ {reverted_name} передумал — снова «{group_name}» "
                f"{filled}/{needed}.",
            )
        except Exception:  # noqa: BLE001
            pass


async def _is_already_claimed(call_id: int, person_id: int) -> bool:
    async with session_scope() as s:
        from sqlalchemy import select

        from app.db.models import SupportClaim

        row = (
            await s.execute(
                select(SupportClaim).where(
                    SupportClaim.support_call_id == call_id,
                    SupportClaim.person_id == person_id,
                )
            )
        ).scalar_one_or_none()
        return row is not None
