"""Integration tests for the attendance scheduler tick."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.core import alerts
from app.core.config import settings
from app.db.models import Response, SurveyKind, SurveyStatus
from app.db.session import session_scope
from app.scheduler import scheduler as sched
from app.services import admins, cancellations, groups, persons, surveys
from app.services import outbox as outbox_svc
from tests._outbox_helpers import drain_outbox

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


async def _tick(bot) -> None:
    """tick() now only enqueues — drain the outbox through the real sender
    worker so `bot.sent` reflects actual delivery, same as before Phase 4."""
    await sched.tick(bot)
    await drain_outbox(bot)


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
    await _tick(bot)

    assert {m.chat_id for m in bot.sent} == {200, 201}
    async with session_scope() as s:
        survey = await surveys.find_attendance_survey(s, group_id, MONDAY_LESSON)
        assert survey is not None and survey.status == SurveyStatus.open


async def test_tick_poll_is_idempotent(db, freeze) -> None:
    await _make_group()
    freeze(datetime(2026, 5, 25, 9, 0))
    bot = FakeBot()
    await _tick(bot)
    await _tick(bot)
    assert len(bot.sent) == 2  # poll not sent twice


async def test_tick_polls_new_member_on_next_tick(db, freeze) -> None:
    """Member added after the first tick should get the survey on the next one,
    while existing members must not get a duplicate."""
    group_id = await _make_group(n_students=2)
    freeze(datetime(2026, 5, 25, 9, 0))
    bot = FakeBot()
    await _tick(bot)
    assert len(bot.sent) == 2

    async with session_scope() as s:
        group = await groups.get_group(s, group_id)
        await persons.add_to_group(s, group, 300, "newbie", "Новенький")

    await _tick(bot)
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
    await _tick(bot)
    assert [m.chat_id for m in bot.sent] == [200]

    # На следующем tick: 201 НЕ пересылается (помечен как undeliverable).
    await _tick(bot)
    assert [m.chat_id for m in bot.sent] == [200]


async def test_tick_retries_failed_send_next_tick(db, freeze) -> None:
    """If send_message fails, the outbox retries with backoff — force the
    retry due immediately (scheduled_at→now) instead of sleeping in the test,
    then drain again for that person (no tg_message_id stored)."""
    from app.db.models import MessageOutbox
    from app.services.schedule import utc_now

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
    await _tick(bot)
    assert [m.chat_id for m in bot.sent] == [200]  # 201 failed

    async with session_scope() as s:
        row = (
            await s.execute(select(MessageOutbox).where(MessageOutbox.chat_id == 201))
        ).scalar_one()
        row.scheduled_at = utc_now()  # skip the backoff wait for the test
    await drain_outbox(bot)
    await _tick(bot)
    assert [m.chat_id for m in bot.sent] == [200, 201]  # 201 retried, 200 not duplicated


async def test_tick_sends_auto_summary(db, freeze) -> None:
    await _make_group()
    freeze(datetime(2026, 5, 25, 11, 0))  # past the halfway point (10:00)
    bot = FakeBot()
    await _tick(bot)

    teacher_msgs = [m for m in bot.sent if m.chat_id == 111]
    assert len(teacher_msgs) == 1
    assert "итоги опроса" in teacher_msgs[0].text

    await _tick(bot)  # summary must not be re-sent
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
    await _tick(bot)
    assert [m for m in bot.sent if m.chat_id == 111] == []  # сводка не доставлена

    bot.block_admin = False  # препод нажал Start
    # Ретрай ушёл в backoff (реальные секунды) — форсируем due-время вместо
    # реального sleep в тесте (тот же приём, что test_tick_retries_failed_send_next_tick).
    from app.db.models import MessageOutbox
    from app.services.schedule import utc_now
    async with session_scope() as s:
        row = (
            await s.execute(select(MessageOutbox).where(MessageOutbox.chat_id == 111))
        ).scalar_one()
        row.scheduled_at = utc_now()
    await drain_outbox(bot)
    await _tick(bot)
    summaries = [
        m for m in bot.sent if m.chat_id == 111 and "итоги опроса" in m.text
    ]
    assert len(summaries) == 1  # дошла ретраем

    await _tick(bot)  # и больше не дублируется
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
    await _tick(bot)  # 200 получил опрос, 201 помечен -1

    freeze(datetime(2026, 5, 25, 11, 0))  # past halfway → auto-summary
    await _tick(bot)
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
    await _tick(bot)
    assert bot.sent == []


async def test_tick_closes_past_survey(db, freeze) -> None:
    group_id = await _make_group()
    async with session_scope() as s:
        await surveys.get_or_create_survey(
            s, group_id, SurveyKind.attendance, datetime(2026, 5, 18, 19, 0)
        )
    freeze(datetime(2026, 5, 25, 9, 0))
    bot = FakeBot()
    await _tick(bot)
    async with session_scope() as s:
        past = await surveys.find_attendance_survey(
            s, group_id, datetime(2026, 5, 18, 19, 0)
        )
        assert past is not None and past.status == SurveyStatus.closed


async def test_tick_empty_group_creates_survey_without_crash(db, freeze) -> None:
    group_id = await _make_group(n_students=0)
    freeze(datetime(2026, 5, 25, 9, 0))
    bot = FakeBot()
    await _tick(bot)
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
    await _tick(bot)
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
    await _tick(bot)

    reminders = [m for m in bot.sent if "Напоминаю" in m.text]
    assert [m.chat_id for m in reminders] == [201]


async def test_tick_sends_1h_reminder_to_pending(db, freeze) -> None:
    """В 1h-окне (0 < until <= 1ч) — отдельная волна."""
    await _make_group(n_students=2)
    freeze(datetime(2026, 5, 25, 9, 0))
    bot = FakeBot()
    await _tick(bot)

    # Tick за 30 минут до занятия — оба ещё молчат.
    freeze(datetime(2026, 5, 25, 18, 30))
    bot = FakeBot()
    await _tick(bot)
    reminders = [m for m in bot.sent if "Напоминаю" in m.text]
    assert sorted(m.chat_id for m in reminders) == [200, 201]


async def test_tick_reminder_idempotent_within_window(db, freeze) -> None:
    """Два tick'а в одном окне → один пинг каждому."""
    await _make_group(n_students=2)
    freeze(datetime(2026, 5, 25, 9, 0))
    await _tick(FakeBot())

    freeze(datetime(2026, 5, 25, 16, 30))  # 4h-окно
    bot = FakeBot()
    await _tick(bot)
    await _tick(bot)
    reminders = [m for m in bot.sent if "Напоминаю" in m.text]
    assert sorted(m.chat_id for m in reminders) == [200, 201]


async def test_tick_4h_and_1h_are_separate_waves(db, freeze) -> None:
    """4h и 1h — независимые окна; кто получил 4h, получит и 1h, если так и не ответил."""
    await _make_group(n_students=1)
    freeze(datetime(2026, 5, 25, 9, 0))
    bot = FakeBot()
    await _tick(bot)
    assert len(bot.sent) == 1  # initial poll

    freeze(datetime(2026, 5, 25, 16, 30))  # 4h-окно
    await _tick(bot)
    assert sum(1 for m in bot.sent if "Напоминаю" in m.text) == 1

    freeze(datetime(2026, 5, 25, 18, 30))  # 1h-окно
    await _tick(bot)
    assert sum(1 for m in bot.sent if "Напоминаю" in m.text) == 2


async def test_first_poll_in_4h_window_does_not_double_send(db, freeze) -> None:
    """Группа создалась поздно, первая рассылка попадает в 4h-окно. Опрос должен
    уйти, а reminder того же tick'a не должен догнаться повторно."""
    await _make_group(n_students=1, lead_hours=5)
    freeze(datetime(2026, 5, 25, 16, 30))  # за 2:30 до занятия
    bot = FakeBot()
    await _tick(bot)
    student_msgs = [m for m in bot.sent if m.chat_id == 200]
    # Опрос ушёл — но он в форме «Придёшь?», не «Напоминаю».
    assert len(student_msgs) == 1
    assert "Напоминаю" not in student_msgs[0].text
    # Следующий tick в том же 4h-окне — ученику ничего нового.
    await _tick(bot)
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
    await _tick(bot)

    pings = [m for m in bot.sent if "никто из приглашённых" in m.text]
    assert len(pings) == 1
    assert pings[0].chat_id == 111

    # Второй tick — повторно НЕ пингуем (no_reply_notified=True)
    await _tick(bot)
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
    await _tick(bot)
    assert sum(1 for m in bot.sent if m.chat_id == 300) == 1  # только обычный опрос
    assert not any("соскучились" in m.text for m in bot.sent if m.chat_id == 300)

    # Второй тик (15 мин спустя, тот же now): survey/Response уже есть,
    # лапсед ещё не ответил — теперь реактивация корректно срабатывает.
    await _tick(bot)
    assert any(
        m.chat_id == 300 and "соскучились" in m.text for m in bot.sent
    )


async def test_heartbeat_tick_writes_atomic_timestamp(tmp_path, monkeypatch) -> None:
    hb = tmp_path / "sub" / "heartbeat"
    monkeypatch.setattr(sched, "HEARTBEAT_FILE", hb)
    await sched._heartbeat_tick()
    assert hb.exists()
    assert not hb.with_suffix(".tmp").exists()  # tmp-файл не остался — os.replace отработал
    ts = datetime.fromisoformat(hb.read_text(encoding="utf-8"))
    assert ts.tzinfo is not None  # aware UTC, не naive


async def test_backfill_creates_closed_survey_for_missed_lesson(db) -> None:
    """Занятие целиком прошло во время даунтайма (никакой tick его не
    застал) — backfill находит его по расписанию и заводит closed survey
    без Response-строк."""
    group_id = await _make_group(n_students=2)
    now = MONDAY_LESSON + timedelta(hours=2)  # бот «ожил» через 2ч после занятия
    created = await sched.backfill_missed_surveys(now)
    assert created == 1

    async with session_scope() as s:
        survey = await surveys.find_attendance_survey(s, group_id, MONDAY_LESSON)
        assert survey is not None
        assert survey.status == SurveyStatus.closed
        assert survey.summary_sent is True
        responses = (await s.execute(
            select(Response).where(Response.survey_id == survey.id)
        )).scalars().all()
        assert responses == []  # никому не начислен «прогул»


async def test_backfill_skips_cancelled_lesson(db) -> None:
    await _make_group(n_students=2)
    async with session_scope() as s:
        await cancellations.cancel(s, (await groups.active_groups(s))[0].id, MONDAY_LESSON)
    now = MONDAY_LESSON + timedelta(hours=2)
    created = await sched.backfill_missed_surveys(now)
    assert created == 0


async def test_backfill_skips_existing_survey(db) -> None:
    """Обычный тик уже успел завести survey (например открыл его до сбоя) —
    backfill не должен его трогать/пересоздавать."""
    group_id = await _make_group(n_students=2)
    async with session_scope() as s:
        survey, _ = await surveys.get_or_create_survey(
            s, group_id, SurveyKind.attendance, MONDAY_LESSON
        )
        assert survey.status == SurveyStatus.open

    now = MONDAY_LESSON + timedelta(hours=2)
    created = await sched.backfill_missed_surveys(now)
    assert created == 0
    async with session_scope() as s:
        survey = await surveys.find_attendance_survey(s, group_id, MONDAY_LESSON)
        assert survey.status == SurveyStatus.open  # backfill не тронул статус


async def test_backfill_skips_lessons_older_than_lookback(db) -> None:
    await _make_group(n_students=2)
    now = MONDAY_LESSON + timedelta(days=3)  # занятие вне 24ч lookback
    created = await sched.backfill_missed_surveys(now)
    assert created == 0
    async with session_scope() as s:
        survey = await surveys.find_attendance_survey(
            s, (await groups.active_groups(s))[0].id, MONDAY_LESSON
        )
        assert survey is None


async def test_backfill_is_idempotent(db) -> None:
    await _make_group(n_students=2)
    now = MONDAY_LESSON + timedelta(hours=2)
    assert await sched.backfill_missed_surveys(now) == 1
    assert await sched.backfill_missed_surveys(now) == 0  # повторный проход ничего не создаёт


async def test_tick_backfills_missed_lesson_without_polling_it(db, freeze) -> None:
    """Полный путь через tick(): занятие пропущено (даунтайм), backfill
    заводит closed survey, а обычный per-group проход его не трогает (он уже
    в прошлом) — никому не уходит поздний опрос по нему."""
    group_id = await _make_group(n_students=2)
    freeze(MONDAY_LESSON + timedelta(hours=2))
    bot = FakeBot()
    await _tick(bot)

    assert bot.sent == []  # занятие уже прошло — поздний опрос никому не ушёл
    async with session_scope() as s:
        survey = await surveys.find_attendance_survey(s, group_id, MONDAY_LESSON)
        assert survey is not None and survey.status == SurveyStatus.closed


async def test_tick_logs_structured_metrics(db, freeze, caplog) -> None:
    """Phase 7: один tick_metrics лог на тик с total/per-stage длительностями
    и счётчиками (groups_processed/surveys_touched/recipients_enqueued) —
    формат, который devstats.py (Phase 9) будет парсить."""
    await _make_group(n_students=2)
    freeze(datetime(2026, 5, 25, 9, 0))  # то же окно, что и в test_tick_sends_poll_within_window
    bot = FakeBot()
    with caplog.at_level("INFO", logger="app.scheduler.scheduler"):
        await sched.tick(bot)

    metrics_lines = [
        r.getMessage() for r in caplog.records if r.getMessage().startswith("tick_metrics")
    ]
    assert len(metrics_lines) == 1
    line = metrics_lines[0]
    assert "groups_processed=1" in line
    assert "surveys_touched=1" in line
    assert "recipients_enqueued=2" in line  # 2 ученика получили опрос
    assert "stage_poll=" in line and "stage_recruit=" in line
    assert "total=" in line


async def test_tick_warns_on_slow_stage(db, freeze, monkeypatch, caplog) -> None:
    """Synthetic slow tick: порог занижен до долей секунды, один stage
    (backfill) реально спит дольше него — подтверждает, что warning
    срабатывает от фактической длительности, а не только по факту вызова."""
    monkeypatch.setattr(sched, "SLOW_TICK_THRESHOLD", timedelta(seconds=0.05))
    original_backfill = sched.backfill_missed_surveys

    async def _slow_backfill(now):
        await asyncio.sleep(0.15)
        return await original_backfill(now)

    monkeypatch.setattr(sched, "backfill_missed_surveys", _slow_backfill)
    await _make_group(n_students=2)
    freeze(datetime(2026, 5, 25, 9, 0))
    bot = FakeBot()
    with caplog.at_level("WARNING", logger="app.scheduler.scheduler"):
        await sched.tick(bot)

    slow_lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("tick_slow")]
    assert len(slow_lines) == 1
    assert "threshold=0s" in slow_lines[0]


async def test_slow_tick_triggers_alert_with_anti_spam(db, freeze, monkeypatch, tmp_path) -> None:
    """Phase 9: медленный тик не только логирует warning (Phase 7), но и
    алертит разработчика — с тем же анти-спам окном, что у watchdog_bot.ps1
    (второй подряд медленный тик не должен продублировать алерт)."""
    monkeypatch.setattr(alerts, "ALERT_STATE_DIR", tmp_path)
    monkeypatch.setattr(settings, "developer_ids", [777])
    monkeypatch.setattr(sched, "SLOW_TICK_THRESHOLD", timedelta(seconds=0.05))
    original_backfill = sched.backfill_missed_surveys

    async def _slow_backfill(now):
        await asyncio.sleep(0.15)
        return await original_backfill(now)

    monkeypatch.setattr(sched, "backfill_missed_surveys", _slow_backfill)
    await _make_group(n_students=2)
    freeze(datetime(2026, 5, 25, 9, 0))
    bot = FakeBot()

    await sched.tick(bot)
    first = [m for m in bot.sent if m.chat_id == 777]
    assert len(first) == 1
    assert "Медленный тик" in first[0].text

    await sched.tick(bot)  # второй медленный тик в то же анти-спам окно
    second = [m for m in bot.sent if m.chat_id == 777]
    assert len(second) == 1  # алерт не продублирован


async def test_stuck_outbox_triggers_alert_with_anti_spam(db, monkeypatch, tmp_path) -> None:
    """Phase 9: застрявший outbox (порог пройден) алертит разработчика один
    раз за анти-спам окно — второй прогон проверки в то же окно молчит."""
    monkeypatch.setattr(alerts, "ALERT_STATE_DIR", tmp_path)
    monkeypatch.setattr(settings, "developer_ids", [777])
    monkeypatch.setattr(sched, "OUTBOX_STUCK_PENDING", 1)  # низкий порог — легко перейти в тесте
    async with session_scope() as s:
        await outbox_svc.enqueue(s, "stuck-1", 1, {"text": "x"})

    bot = FakeBot()
    await sched._check_outbox_health(bot)
    first = [m for m in bot.sent if m.chat_id == 777]
    assert len(first) == 1
    assert "Outbox застрял" in first[0].text

    await sched._check_outbox_health(bot)  # вторая проверка в то же окно
    second = [m for m in bot.sent if m.chat_id == 777]
    assert len(second) == 1  # алерт не продублирован


async def test_outbox_not_stuck_below_threshold_no_alert(db, monkeypatch, tmp_path) -> None:
    """Негативный кейс: пока pending ниже порога и очередь свежая — алерта нет."""
    monkeypatch.setattr(alerts, "ALERT_STATE_DIR", tmp_path)
    monkeypatch.setattr(settings, "developer_ids", [777])
    async with session_scope() as s:
        await outbox_svc.enqueue(s, "fresh-1", 1, {"text": "x"})

    bot = FakeBot()
    await sched._check_outbox_health(bot)
    assert [m for m in bot.sent if m.chat_id == 777] == []
