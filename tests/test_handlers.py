"""Tests for the attendance callback handler."""
from datetime import datetime
from types import SimpleNamespace

from app.bot.handlers.survey import on_attendance
from app.db.models import SurveyKind
from app.db.session import session_scope
from app.services import groups, persons, surveys, admins

LESSON = datetime(2026, 5, 25, 19, 0)


class FakeCbMessage:
    def __init__(self) -> None:
        self.text: str | None = None

    async def edit_text(self, text, **kwargs) -> None:
        self.text = text


class FakeCallback:
    def __init__(self, data: str, user_id: int) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeCbMessage()
        self.answers: list[tuple[str, bool]] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


async def _survey_with_member() -> int:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 1, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        p, _ = await persons.add_to_group(s, g, 700, "u", "Ученик")
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, LESSON
        )
        await surveys.ensure_responses(s, survey.id, [p.id])
        return survey.id


async def test_attendance_answer_recorded(db) -> None:
    survey_id = await _survey_with_member()
    call = FakeCallback(f"att:{survey_id}:yes", user_id=700)
    await on_attendance(call)
    assert call.message.text is not None and "придёшь" in call.message.text
    async with session_scope() as s:
        summary = await surveys.summarize(s, survey_id)
    assert (summary.yes, summary.no) == (1, 0)


async def test_attendance_rejects_non_member(db) -> None:
    survey_id = await _survey_with_member()
    call = FakeCallback(f"att:{survey_id}:yes", user_id=999)
    await on_attendance(call)
    assert call.answers and "Тебя нет" in call.answers[0][0]
    async with session_scope() as s:
        summary = await surveys.summarize(s, survey_id)
    assert summary.yes == 0


async def test_attendance_rejects_closed_survey(db) -> None:
    survey_id = await _survey_with_member()
    async with session_scope() as s:
        await surveys.close_past_surveys(s, datetime(2030, 1, 1))
    call = FakeCallback(f"att:{survey_id}:yes", user_id=700)
    await on_attendance(call)
    assert call.answers and "закрыт" in call.answers[0][0]


async def test_attendance_unknown_survey(db) -> None:
    await _survey_with_member()
    call = FakeCallback("att:99999:yes", user_id=700)
    await on_attendance(call)
    assert call.answers and "не найден" in call.answers[0][0]
