"""Integration tests for the attendance scheduler tick."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.db.models import SurveyKind, SurveyStatus
from app.db.session import session_scope
from app.scheduler import scheduler as sched
from app.services import admins, groups, persons, surveys

MONDAY_LESSON = datetime(2026, 5, 25, 19, 0)


class FakeBot:
    """Records send_message calls; mimics the bits of aiogram.Bot the scheduler uses."""

    def __init__(self) -> None:
        self.sent: list[SimpleNamespace] = []
        self._mid = 1000

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self._mid += 1
        msg = SimpleNamespace(
            chat_id=chat_id, text=text, reply_markup=reply_markup, message_id=self._mid
        )
        self.sent.append(msg)
        return msg


@pytest.fixture
def freeze(monkeypatch):
    """Pin the scheduler's clock so tick() is deterministic."""

    def _set(dt: datetime) -> None:
        monkeypatch.setattr(sched, "local_now", lambda: dt)

    return _set


async def _make_group(lead_hours: int = 18, n_students: int = 2) -> int:
    async with session_scope() as s:
        teacher = await admins.get_or_create(s, 111, "t", "Препод")
        group = await groups.create_group(
            s, teacher.id, "Группа A", "0", "19:00", lead_hours
        )
        for i in range(n_students):
            await persons.add_to_group(s, group, 200 + i, f"u{i}", f"Ученик {i}")
        return group.id


async def test_tick_sends_poll_within_window(db, freeze) -> None:
    group_id = await _make_group()
    freeze(datetime(2026, 5, 25, 9, 0))  # 10h before lesson, before summary halfway
    bot = FakeBot()
    await sched.tick(bot)

    assert {m.chat_id for m in bot.sent} == {200, 201}
    async with session_scope() as s:
        survey = await surveys.find_attendance_survey(s, group_id, MONDAY_LESSON)
        assert survey is not None and survey.status == SurveyStatus.open


async def test_tick_poll_is_idempotent(db, freeze) -> None:
    await _make_group()
    freeze(datetime(2026, 5, 25, 9, 0))
    bot = FakeBot()
    await sched.tick(bot)
    await sched.tick(bot)
    assert len(bot.sent) == 2  # poll not sent twice


async def test_tick_polls_new_member_on_next_tick(db, freeze) -> None:
    """Member added after the first tick should get the survey on the next one,
    while existing members must not get a duplicate."""
    group_id = await _make_group(n_students=2)
    freeze(datetime(2026, 5, 25, 9, 0))
    bot = FakeBot()
    await sched.tick(bot)
    assert len(bot.sent) == 2

    async with session_scope() as s:
        group = await groups.get_group(s, group_id)
        await persons.add_to_group(s, group, 300, "newbie", "Новенький")

    await sched.tick(bot)
    assert [m.chat_id for m in bot.sent] == [200, 201, 300]


async def test_tick_retries_failed_send_next_tick(db, freeze) -> None:
    """If send_message fails, next tick must retry for that person (no tg_message_id stored)."""
    await _make_group(n_students=2)
    freeze(datetime(2026, 5, 25, 9, 0))

    class FlakyBot(FakeBot):
        def __init__(self) -> None:
            super().__init__()
            self.fail_once = {201}

        async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
            if chat_id in self.fail_once:
                self.fail_once.discard(chat_id)
                raise RuntimeError("simulated telegram outage")
            return await super().send_message(chat_id, text, reply_markup, **kwargs)

    bot = FlakyBot()
    await sched.tick(bot)
    assert [m.chat_id for m in bot.sent] == [200]  # 201 failed

    await sched.tick(bot)
    assert [m.chat_id for m in bot.sent] == [200, 201]  # 201 retried, 200 not duplicated


async def test_tick_sends_auto_summary(db, freeze) -> None:
    await _make_group()
    freeze(datetime(2026, 5, 25, 11, 0))  # past the halfway point (10:00)
    bot = FakeBot()
    await sched.tick(bot)

    teacher_msgs = [m for m in bot.sent if m.chat_id == 111]
    assert len(teacher_msgs) == 1
    assert "итоги опроса" in teacher_msgs[0].text

    await sched.tick(bot)  # summary must not be re-sent
    assert len([m for m in bot.sent if m.chat_id == 111]) == 1


async def test_tick_no_poll_outside_window(db, freeze) -> None:
    await _make_group(lead_hours=18)
    freeze(datetime(2026, 5, 24, 9, 0))  # Sunday, ~34h before lesson
    bot = FakeBot()
    await sched.tick(bot)
    assert bot.sent == []


async def test_tick_closes_past_survey(db, freeze) -> None:
    group_id = await _make_group()
    async with session_scope() as s:
        await surveys.get_or_create_survey(
            s, group_id, SurveyKind.attendance, datetime(2026, 5, 18, 19, 0)
        )
    freeze(datetime(2026, 5, 25, 9, 0))
    bot = FakeBot()
    await sched.tick(bot)
    async with session_scope() as s:
        past = await surveys.find_attendance_survey(
            s, group_id, datetime(2026, 5, 18, 19, 0)
        )
        assert past is not None and past.status == SurveyStatus.closed


async def test_tick_empty_group_creates_survey_without_crash(db, freeze) -> None:
    group_id = await _make_group(n_students=0)
    freeze(datetime(2026, 5, 25, 9, 0))
    bot = FakeBot()
    await sched.tick(bot)
    assert bot.sent == []
    async with session_scope() as s:
        survey = await surveys.find_attendance_survey(s, group_id, MONDAY_LESSON)
        assert survey is not None
