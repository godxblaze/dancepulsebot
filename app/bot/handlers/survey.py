from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.db.models import Answer, SurveyStatus
from app.db.session import session_scope
from app.services import students as students_svc
from app.services import surveys as surveys_svc

log = logging.getLogger(__name__)
router = Router()


@router.callback_query(F.data.startswith("att:"))
async def on_attendance(call: CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    survey_id = int(parts[1])
    answer = Answer.yes if parts[2] == "yes" else Answer.no

    async with session_scope() as s:
        survey = await surveys_svc.get_survey(s, survey_id)
        if survey is None:
            await call.answer("Опрос не найден", show_alert=True)
            return
        if survey.status == SurveyStatus.closed:
            await call.answer("Опрос уже закрыт — занятие прошло", show_alert=True)
            return
        student = await students_svc.get_in_group(s, survey.group_id, call.from_user.id)
        if student is None:
            await call.answer("Тебя нет в этой группе", show_alert=True)
            return
        await surveys_svc.record_answer(s, survey_id, student.id, answer)

    text = (
        "✅ Записал: придёшь на занятие."
        if answer == Answer.yes
        else "❌ Записал: не придёшь."
    )
    if call.message:
        try:
            await call.message.edit_text(text)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not edit survey message: %s", e)
    await call.answer()
