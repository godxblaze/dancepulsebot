"""Tests for the attendance callback handler."""
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.bot.handlers.survey import on_attendance
from app.db.models import Gender, SurveyKind
from app.db.session import session_scope
from app.services import admins, groups, persons, surveys

_MSK = ZoneInfo("Europe/Moscow")
LESSON = datetime(2026, 5, 25, 19, 0, tzinfo=_MSK).astimezone(UTC)


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


async def test_attendance_shows_who_is_coming_by_gender(db) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 1, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        boris, _ = await persons.add_to_group(s, g, 700, "u", "Боря")
        await persons.set_gender(s, boris.id, Gender.male)
        anya, _ = await persons.add_to_group(s, g, 701, "u", "Аня")
        await persons.set_gender(s, anya.id, Gender.female)
        masha, _ = await persons.add_to_group(s, g, 702, "u", "Маша")
        await persons.set_gender(s, masha.id, Gender.female)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, LESSON
        )
        await surveys.ensure_responses(s, survey.id, [boris.id, anya.id, masha.id])
        survey_id = survey.id

    call = None
    for uid in (700, 701, 702):
        call = FakeCallback(f"att:{survey_id}:yes", user_id=uid)
        await on_attendance(call)

    text = call.message.text
    assert "Идут (3)" in text
    assert "👨 1: Боря" in text
    assert "👩 2:" in text
    assert "Аня" in text and "Маша" in text


async def test_attendance_no_answer_shows_others_excludes_self(db) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 1, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        boris, _ = await persons.add_to_group(s, g, 700, "u", "Боря")
        await persons.set_gender(s, boris.id, Gender.male)
        oleg, _ = await persons.add_to_group(s, g, 701, "u", "Олег")
        await persons.set_gender(s, oleg.id, Gender.male)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, LESSON
        )
        await surveys.ensure_responses(s, survey.id, [boris.id, oleg.id])
        survey_id = survey.id

    await on_attendance(FakeCallback(f"att:{survey_id}:yes", user_id=700))  # Боря идёт
    call = FakeCallback(f"att:{survey_id}:no", user_id=701)  # Олег — нет
    await on_attendance(call)
    assert "не придёшь" in call.message.text
    assert "Идут (1):" in call.message.text and "👨 1: Боря" in call.message.text
    assert "Олег" not in call.message.text  # но не себя


async def test_attendance_no_answer_empty_roster_no_placeholder(db) -> None:
    survey_id = await _survey_with_member()  # один участник, ещё никто не идёт
    call = FakeCallback(f"att:{survey_id}:no", user_id=700)
    await on_attendance(call)
    assert "не придёшь" in call.message.text
    assert "Идут" not in call.message.text
    assert "будь первым" not in call.message.text


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
