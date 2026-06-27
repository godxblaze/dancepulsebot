from __future__ import annotations

import logging
from html import escape

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery

from app.bot import ui
from app.bot.keyboards import flip_answer_keyboard
from app.bot.utils import format_group_title
from app.db.models import Answer, SurveyStatus
from app.db.session import session_scope
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import schools as schools_svc
from app.services import surveys as surveys_svc
from app.services.schedule import format_lesson_at

log = logging.getLogger(__name__)
router = Router()


@router.callback_query(F.data.startswith("att:"))
async def on_attendance(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    survey_id = int(parts[1])
    choice = parts[2]  # "yes" or "no"
    answer = Answer.yes if choice == "yes" else Answer.no

    async with session_scope() as s:
        survey = await surveys_svc.get_survey(s, survey_id)
        if survey is None:
            await call.answer("Опрос не найден", show_alert=True)
            return
        if survey.status == SurveyStatus.closed:
            await call.answer("Опрос уже закрыт — занятие прошло", show_alert=True)
            return
        person = await persons_svc.get_in_group(s, survey.group_id, call.from_user.id)
        if person is None:
            await call.answer("Тебя нет в этой группе", show_alert=True)
            return
        await surveys_svc.record_answer(s, survey_id, person.id, answer)
        roster = await surveys_svc.coming_by_gender(s, survey_id)
        group = await groups_svc.get_group(s, survey.group_id)
        title = escape(format_group_title(group)) if group is not None else "занятие"
        lesson_at = survey.lesson_at
        tz_name = (
            await schools_svc.get_timezone(s, group.school_id)
            if group and group.school_id else None
        )

    when = format_lesson_at(lesson_at, tz=tz_name)
    # B2: на «не приду» — мягкий commitment-нудж (флип-кнопка ниже остаётся).
    text = (
        f"{ui.YES} Записал: придёшь на «{title}» {when}. Партнёр будет рад!"
        if answer == Answer.yes
        else f"{ui.NO} Записал: не придёшь на «{title}» {when}.\n"
        f"Жаль! {ui.COMMITMENT} Передумаешь — жми ниже."
    )
    coming = ui.format_coming(roster)
    if coming:
        text += "\n\n" + coming
    if call.message:
        try:
            await call.message.edit_text(
                text,
                reply_markup=flip_answer_keyboard(survey_id, choice),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Could not edit survey message: %s", e)
    await call.answer()
