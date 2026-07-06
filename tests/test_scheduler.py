"""Integration tests for the attendance scheduler tick."""
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.db.models import SurveyKind, SurveyStatus
from app.db.session import session_scope
from app.scheduler import scheduler as sched
from app.services import admins, groups, persons, surveys

# Школа в Europe/Moscow (UTC+3). Тесты пишут датавремя в местном
# восприятии «19:00 в понедельник в Москве», а в БД и сравнениях оно живёт
# как aware UTC = 16:00.
_MSK = ZoneInfo("Europe/Moscow")


def _msk(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Aware UTC datetime от человекочитаемого МСК-времени."""
    return datetime(year, month, day, hour, minute, tzinfo=_MSK).astimezone(UTC)


MONDAY_LESSON = _msk(2026, 5, 25, 19, 0)


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
    """Pin the scheduler's clock so tick() is deterministic. Принимает datetime
    в любом виде: aware → используется как есть, naive → считаем что это МСК
    и конвертим в UTC."""

    def _set(dt: datetime) -> None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_MSK).astimezone(UTC)
        monkeypatch.setattr(sched, "utc_now", lambda: dt)

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


async def test_tick_marks_blocker_as_undeliverable(db, freeze) -> None:
    """TelegramForbiddenError — пользователь заблокировал бота. Помечаем -1,
    больше не пытаемся, никакого спама логов."""
    from aiogram.exceptions import TelegramForbiddenError

    await _make_group(n_students=2)
    freeze(datetime(2026, 5, 25, 9, 0))

    class BlockerBot(FakeBot):
        async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
            if chat_id == 201:
                raise TelegramForbiddenError(method=None, message="bot was blocked by the user")
            return await super().send_message(chat_id, text, reply_markup, **kwargs)

    bot = BlockerBot()
    await sched.tick(bot)
    assert [m.chat_id for m in bot.sent] == [200]

    # На следующем tick: 201 НЕ пересылается (помечен как undeliverable).
    await sched.tick(bot)
    assert [m.chat_id for m in bot.sent] == [200]


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


async def test_auto_summary_retries_when_admin_blocked(db, freeze) -> None:
    """Препод заблокировал бота / не нажал Start (terminal) — summary НЕ метится
    sent, дойдёт на следующем tick'е, когда препод станет доступен."""
    from aiogram.exceptions import TelegramForbiddenError

    await _make_group()
    freeze(datetime(2026, 5, 25, 11, 0))  # past the halfway point

    class AdminBlockedOnce(FakeBot):
        def __init__(self) -> None:
            super().__init__()
            self.block_admin = True

        async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
            if chat_id == 111 and self.block_admin:
                raise TelegramForbiddenError(
                    method=None, message="bot was blocked by the user"
                )
            return await super().send_message(chat_id, text, reply_markup, **kwargs)

    bot = AdminBlockedOnce()
    await sched.tick(bot)
    assert [m for m in bot.sent if m.chat_id == 111] == []  # сводка не доставлена

    bot.block_admin = False  # препод нажал Start
    await sched.tick(bot)
    summaries = [
        m for m in bot.sent if m.chat_id == 111 and "итоги опроса" in m.text
    ]
    assert len(summaries) == 1  # дошла ретраем

    await sched.tick(bot)  # и больше не дублируется
    summaries = [
        m for m in bot.sent if m.chat_id == 111 and "итоги опроса" in m.text
    ]
    assert len(summaries) == 1


async def test_summary_shows_undelivered_not_pending(db, freeze) -> None:
    """Автосводка: недоставленный (заблокировал бота) — не «молчит», а
    отдельная строка «не дошло»; в «молчат» его имени нет."""
    from aiogram.exceptions import TelegramForbiddenError

    await _make_group(n_students=2)
    freeze(datetime(2026, 5, 25, 9, 0))  # до halfway — только опрос

    class BlockerBot(FakeBot):
        async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
            if chat_id == 201:
                raise TelegramForbiddenError(method=None, message="bot was blocked by the user")
            return await super().send_message(chat_id, text, reply_markup, **kwargs)

    bot = BlockerBot()
    await sched.tick(bot)  # 200 получил опрос, 201 помечен -1

    freeze(datetime(2026, 5, 25, 11, 0))  # past halfway → auto-summary
    await sched.tick(bot)
    summaries = [m for m in bot.sent if m.chat_id == 111 and "итоги опроса" in m.text]
    assert len(summaries) == 1
    text = summaries[0].text
    assert "не дошло: 1" in text
    assert "молчат" in text and "Ученик 0" in text  # 200 — реально молчит
    assert "Ученик 1" not in text  # 201 (заблокировал) не показан как молчащий


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


async def test_tick_sends_4h_reminder_to_pending(db, freeze) -> None:
    """В 4h-окне (1ч < until <= 4ч) молчащим уходит «⏰ Напоминаю»."""
    await _make_group(n_students=2)
    # Первый tick за ~10ч до занятия — раздаёт опросы.
    freeze(datetime(2026, 5, 25, 9, 0))
    bot = FakeBot()
    await sched.tick(bot)
    assert len(bot.sent) == 2

    # Студент 200 ответил, 201 молчит.
    async with session_scope() as s:
        from app.db.models import Answer
        survey = await surveys.find_attendance_survey(s, 1, MONDAY_LESSON)
        person = await persons.get_in_group(s, 1, 200)
        await surveys.record_answer(s, survey.id, person.id, Answer.yes)

    # Tick в 4h-окне (16:30 — до занятия ровно 2:30).
    freeze(datetime(2026, 5, 25, 16, 30))
    bot = FakeBot()
    await sched.tick(bot)

    reminders = [m for m in bot.sent if "Напоминаю" in m.text]
    assert [m.chat_id for m in reminders] == [201]


async def test_tick_sends_1h_reminder_to_pending(db, freeze) -> None:
    """В 1h-окне (0 < until <= 1ч) — отдельная волна."""
    await _make_group(n_students=2)
    freeze(datetime(2026, 5, 25, 9, 0))
    bot = FakeBot()
    await sched.tick(bot)

    # Tick за 30 минут до занятия — оба ещё молчат.
    freeze(datetime(2026, 5, 25, 18, 30))
    bot = FakeBot()
    await sched.tick(bot)
    reminders = [m for m in bot.sent if "Напоминаю" in m.text]
    assert sorted(m.chat_id for m in reminders) == [200, 201]


async def test_tick_reminder_idempotent_within_window(db, freeze) -> None:
    """Два tick'а в одном окне → один пинг каждому."""
    await _make_group(n_students=2)
    freeze(datetime(2026, 5, 25, 9, 0))
    await sched.tick(FakeBot())

    freeze(datetime(2026, 5, 25, 16, 30))  # 4h-окно
    bot = FakeBot()
    await sched.tick(bot)
    await sched.tick(bot)
    reminders = [m for m in bot.sent if "Напоминаю" in m.text]
    assert sorted(m.chat_id for m in reminders) == [200, 201]


async def test_tick_4h_and_1h_are_separate_waves(db, freeze) -> None:
    """4h и 1h — независимые окна; кто получил 4h, получит и 1h, если так и не ответил."""
    await _make_group(n_students=1)
    freeze(datetime(2026, 5, 25, 9, 0))
    bot = FakeBot()
    await sched.tick(bot)
    assert len(bot.sent) == 1  # initial poll

    freeze(datetime(2026, 5, 25, 16, 30))  # 4h-окно
    await sched.tick(bot)
    assert sum(1 for m in bot.sent if "Напоминаю" in m.text) == 1

    freeze(datetime(2026, 5, 25, 18, 30))  # 1h-окно
    await sched.tick(bot)
    assert sum(1 for m in bot.sent if "Напоминаю" in m.text) == 2


async def test_first_poll_in_4h_window_does_not_double_send(db, freeze) -> None:
    """Группа создалась поздно, первая рассылка попадает в 4h-окно. Опрос должен
    уйти, а reminder того же tick'a не должен догнаться повторно."""
    await _make_group(n_students=1, lead_hours=5)
    freeze(datetime(2026, 5, 25, 16, 30))  # за 2:30 до занятия
    bot = FakeBot()
    await sched.tick(bot)
    student_msgs = [m for m in bot.sent if m.chat_id == 200]
    # Опрос ушёл — но он в форме «Придёшь?», не «Напоминаю».
    assert len(student_msgs) == 1
    assert "Напоминаю" not in student_msgs[0].text
    # Следующий tick в том же 4h-окне — ученику ничего нового.
    await sched.tick(bot)
    assert len([m for m in bot.sent if m.chat_id == 200]) == 1


async def test_tick_pings_admin_when_no_replies_to_support(db, freeze) -> None:
    """Если SupportCall в open уже >1h без откликов, scheduler пингует админа."""
    from datetime import UTC, timedelta

    from app.db.models import Gender, SupportCall, SupportCallStatus

    group_id = await _make_group(n_students=0)
    async with session_scope() as s:
        survey, _ = await surveys.get_or_create_survey(
            s, group_id, SurveyKind.attendance, MONDAY_LESSON
        )
        call = SupportCall(
            survey_id=survey.id,
            gender=Gender.male,
            needed=2,
            status=SupportCallStatus.open,
        )
        s.add(call)
        await s.flush()
        # Имитируем «прошёл час с создания»
        call.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2)
        await s.flush()
        call_id = call.id

    # tick за пределами окна опросов (до открытия, не после занятия — иначе
    # нудж «напиши лично» сам по себе подавляется гвардом «занятие уже прошло»)
    freeze(datetime(2026, 5, 23, 6, 0))
    bot = FakeBot()
    await sched.tick(bot)

    pings = [m for m in bot.sent if "никто из приглашённых" in m.text]
    assert len(pings) == 1
    assert pings[0].chat_id == 111

    # Второй tick — повторно НЕ пингуем (no_reply_notified=True)
    await sched.tick(bot)
    assert sum(1 for m in bot.sent if "никто из приглашённых" in m.text) == 1
    # Sanity: call помечен
    async with session_scope() as s:
        c = await s.get(SupportCall, call_id)
        assert c.no_reply_notified is True


async def test_reactivate_does_not_double_message_freshly_polled_lapsed(db, freeze) -> None:
    """Лапседа (пропустил REACTIVATE_MISSES=2 закрытых занятия) не должно
    задваивать сообщениями в тот же тик, где ему только что ушёл обычный
    опрос — _maybe_reactivate теперь идёт ДО _poll_group, так что до
    появления survey текущего занятия он тихо выходит (find_attendance_survey
    вернёт None), а не бьёт «давно не видели» через секунду после «придёшь?»."""
    from app.db.models import Answer

    group_id = await _make_group(n_students=0)
    async with session_scope() as s:
        group = await groups.get_group(s, group_id)
        lapsed, _ = await persons.add_to_group(s, group, 300, "u", "Лапсед")
        for when in (_msk(2026, 5, 11, 19, 0), _msk(2026, 5, 18, 19, 0)):
            sv, _ = await surveys.get_or_create_survey(s, group_id, SurveyKind.attendance, when)
            sv.status = SurveyStatus.closed
            await surveys.ensure_responses(s, sv.id, [lapsed.id])
            await surveys.record_answer(s, sv.id, lapsed.id, Answer.no)

    # Первый тик текущего занятия: survey ещё не существует, когда должен
    # отработать _maybe_reactivate (он теперь первый в очереди) — реактивации
    # в этом тике быть не должно, только обычный опрос.
    freeze(datetime(2026, 5, 25, 9, 0))
    bot = FakeBot()
    await sched.tick(bot)
    assert sum(1 for m in bot.sent if m.chat_id == 300) == 1  # только обычный опрос
    assert not any("соскучились" in m.text for m in bot.sent if m.chat_id == 300)

    # Второй тик (15 мин спустя, тот же now): survey/Response уже есть,
    # лапсед ещё не ответил — теперь реактивация корректно срабатывает.
    await sched.tick(bot)
    assert any(
        m.chat_id == 300 and "соскучились" in m.text for m in bot.sent
    )
