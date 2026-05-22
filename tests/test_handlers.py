"""Tests for bot callback / message handlers."""
from datetime import datetime
from types import SimpleNamespace

from app.bot.handlers.student import join_group
from app.bot.handlers.survey import on_attendance
from app.db.models import SurveyKind
from app.db.session import session_scope
from app.services import groups, persons, surveys, teachers

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


class FakeMessage:
    def __init__(self, user_id: int) -> None:
        self.from_user = SimpleNamespace(
            id=user_id, full_name="Тест Юзер", username="tester"
        )
        self.replies: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.replies.append(text)


async def _survey_with_member() -> tuple[int, int]:
    async with session_scope() as s:
        t = await teachers.get_or_create(s, 1, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        p, _ = await persons.add_to_group(s, g, 700, "u", "Ученик")
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, LESSON
        )
        await surveys.ensure_responses(s, survey.id, [p.id])
        return survey.id, p.id


# ── on_attendance ────────────────────────────────────────────────────


async def test_attendance_answer_recorded(db) -> None:
    survey_id, _ = await _survey_with_member()
    call = FakeCallback(f"att:{survey_id}:yes", user_id=700)
    await on_attendance(call)
    assert call.message.text is not None and "придёшь" in call.message.text
    async with session_scope() as s:
        summary = await surveys.summarize(s, survey_id)
    assert (summary.yes, summary.no) == (1, 0)


async def test_attendance_rejects_non_member(db) -> None:
    survey_id, _ = await _survey_with_member()
    call = FakeCallback(f"att:{survey_id}:yes", user_id=999)
    await on_attendance(call)
    assert call.answers and "Тебя нет" in call.answers[0][0]
    async with session_scope() as s:
        summary = await surveys.summarize(s, survey_id)
    assert summary.yes == 0


async def test_attendance_rejects_closed_survey(db) -> None:
    survey_id, _ = await _survey_with_member()
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


# ── join_group ───────────────────────────────────────────────────────


async def test_join_group_success(db) -> None:
    async with session_scope() as s:
        t = await teachers.get_or_create(s, 1, "t", "T")
        g = await groups.create_group(s, t.id, "Сальса", "0", "19:00", 18)
        code, gid = g.join_code, g.id
    msg = FakeMessage(user_id=800)
    await join_group(msg, SimpleNamespace(args=f"join_{code}"))
    assert any("записан" in r for r in msg.replies)
    async with session_scope() as s:
        assert await persons.count_members(s, gid) == 1


async def test_join_group_unknown_code(db) -> None:
    msg = FakeMessage(user_id=801)
    await join_group(msg, SimpleNamespace(args="join_does-not-exist"))
    assert any("не найдена" in r for r in msg.replies)


async def test_join_group_idempotent(db) -> None:
    async with session_scope() as s:
        t = await teachers.get_or_create(s, 1, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        code, gid = g.join_code, g.id
    msg = FakeMessage(user_id=802)
    await join_group(msg, SimpleNamespace(args=f"join_{code}"))
    await join_group(msg, SimpleNamespace(args=f"join_{code}"))
    async with session_scope() as s:
        assert await persons.count_members(s, gid) == 1
    assert any("уже в группе" in r for r in msg.replies)
