"""Место/зал одного занятия (LessonPlaceOverride): сервис, UI-флоу
lesson_place.py, подстановка в опрос scheduler'ом и cleanup при правке
расписания."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.bot.handlers import lesson_place as lp
from app.bot.handlers.lesson_place import (
    cb_lesson_place_ask,
    cb_lesson_place_clear,
    cb_lesson_place_pick,
    lesson_place_typed,
)
from app.bot.states import SetLessonPlace
from app.db.models import Answer, Gender, SupportCallStatus, SurveyKind
from app.db.session import session_scope
from app.scheduler import scheduler as sched
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import place_overrides as overrides_svc
from app.services import schools as schools_svc
from app.services import supports as supports_svc
from app.services import surveys as surveys_svc
from app.services.schedule import upcoming_lessons
from tests._e2e_helpers import LESSON, TEACHER_TG, cb, join_via_link, make_group
from tests._fakes import FakeBot, FakeFSM, FakeMessage, SentMessage, fake_user

_MSK = ZoneInfo("Europe/Moscow")


def _pin_now(monkeypatch, now):
    monkeypatch.setattr(lp, "utc_now", lambda: now)


async def _first_upcoming(gid: int, now) -> datetime:
    async with session_scope() as s:
        group = await groups_svc.get_group(s, gid)
        tz = await schools_svc.get_timezone(s, group.school_id)
        ups = upcoming_lessons(group.weekdays, group.lesson_time, now, n=5, tz=tz)
    assert ups
    return ups[0]


# ── service ──────────────────────────────────────────────────────────


async def test_override_service_upsert_clear_delete_future(db) -> None:
    gid, sid, code = await make_group()
    dt = LESSON
    async with session_scope() as s:
        assert await overrides_svc.get_place(s, gid, dt) is None
        await overrides_svc.set_place(s, gid, dt, "зал 5")
        assert await overrides_svc.get_place(s, gid, dt) == "зал 5"
        # upsert: повторный ввод перезаписывает, не плодит строк
        await overrides_svc.set_place(s, gid, dt, "зал 7")
        assert await overrides_svc.get_place(s, gid, dt) == "зал 7"
        # clear → возврат к месту группы
        assert await overrides_svc.clear(s, gid, dt) is True
        assert await overrides_svc.clear(s, gid, dt) is False
        # delete_future чистит только будущее
        past, future = dt - timedelta(days=30), dt + timedelta(days=7)
        await overrides_svc.set_place(s, gid, past, "старый зал")
        await overrides_svc.set_place(s, gid, future, "новый зал")
        deleted = await overrides_svc.delete_future(s, gid, dt - timedelta(days=1))
        assert deleted == 1
        assert await overrides_svc.get_place(s, gid, past) == "старый зал"
        assert await overrides_svc.get_place(s, gid, future) is None


# ── UI flow ──────────────────────────────────────────────────────────


async def test_set_place_flow_notifies_coming_and_supports(db, monkeypatch) -> None:
    """pick → текст «зал 5» → override записан; «приду»-ученик и заявившийся
    саппорт получили уведомление, препод — сводку."""
    bot = FakeBot()
    gid, sid, code = await make_group()
    now = LESSON - timedelta(hours=6)
    _pin_now(monkeypatch, now)
    dt = await _first_upcoming(gid, now)
    ts = int(dt.timestamp())

    await join_via_link(bot, code, 801, "Аня", Gender.female)
    async with session_scope() as s:
        survey, _ = await surveys_svc.get_or_create_survey(
            s, gid, SurveyKind.attendance, dt
        )
        members = await persons_svc.group_members(s, gid)
        await surveys_svc.ensure_responses(s, survey.id, [p.id for p in members])
        for p in members:
            await surveys_svc.record_answer(s, survey.id, p.id, Answer.yes)
        group = await groups_svc.get_group(s, gid)
        sup, _ = await persons_svc.add_to_group(s, group, 810, "u", "Слава")
        await persons_svc.set_gender(s, sup.id, Gender.male)
        call = await supports_svc.create_call(
            s, survey.id, Gender.male, 2, SupportCallStatus.open
        )
        await supports_svc.claim(s, call.id, sup.id)

    pick = SentMessage(bot, TEACHER_TG, "")
    await cb_lesson_place_pick(cb(f"grpplace:{gid}", TEACHER_TG, message=pick))
    assert "другое место" in pick.text

    state = FakeFSM()
    await cb_lesson_place_ask(
        cb(f"grpplacedt:{gid}:{ts}", TEACHER_TG, message=pick), state
    )
    assert state.state == SetLessonPlace.place
    assert "Где пройдёт занятие" in pick.text

    typed = FakeMessage(fake_user(TEACHER_TG), text="зал 5", bot=bot)
    await lesson_place_typed(typed, state, bot)
    assert state.state is None
    assert typed.replies and "Записал" in typed.replies[-1].text

    async with session_scope() as s:
        assert await overrides_svc.get_place(s, gid, dt) == "зал 5"
    assert any("зал 5" in m.text for m in bot.sent_to(801))
    assert any("зал 5" in m.text for m in bot.sent_to(810))

    # повторный pick показывает текущее место на кнопке
    pick2 = SentMessage(bot, TEACHER_TG, "")
    await cb_lesson_place_pick(cb(f"grpplace:{gid}", TEACHER_TG, message=pick2))
    labels = [
        b.text for row in pick2.reply_markup.inline_keyboard for b in row
    ]
    assert any("зал 5" in t for t in labels)

    # сброс — override исчезает
    await cb_lesson_place_clear(
        cb(f"grpplaceclear:{gid}:{ts}", TEACHER_TG, message=pick2), FakeFSM()
    )
    async with session_scope() as s:
        assert await overrides_svc.get_place(s, gid, dt) is None


async def test_set_place_stale_flow_does_not_write(db, monkeypatch) -> None:
    """Текст спустя >10 мин после открытия шага не пишет override (зеркало
    протухания причины отмены)."""
    bot = FakeBot()
    gid, sid, code = await make_group()
    now = LESSON - timedelta(hours=6)
    _pin_now(monkeypatch, now)
    dt = await _first_upcoming(gid, now)

    state = FakeFSM()
    state.state = SetLessonPlace.place
    await state.update_data(
        group_id=gid, ts=int(dt.timestamp()),
        asked_at=(now - timedelta(minutes=11)).timestamp(),
    )
    typed = FakeMessage(fake_user(TEACHER_TG), text="зал 9", bot=bot)
    await lesson_place_typed(typed, state, bot)
    assert "устарел" in typed.replies[-1].text
    async with session_scope() as s:
        assert await overrides_svc.get_place(s, gid, dt) is None


async def test_foreign_admin_blocked_on_place(db, monkeypatch) -> None:
    """Чужой admin не может открыть pick/задать место чужой группе."""
    bot = FakeBot()
    gid, sid, code = await make_group()
    now = LESSON - timedelta(hours=6)
    _pin_now(monkeypatch, now)
    dt = await _first_upcoming(gid, now)
    ts = int(dt.timestamp())
    async with session_scope() as s:
        from app.services import admins as admins_svc
        await admins_svc.get_or_create(s, 400, "x", "Чужой")

    for data, handler, extra in (
        (f"grpplace:{gid}", cb_lesson_place_pick, ()),
        (f"grpplacedt:{gid}:{ts}", cb_lesson_place_ask, (FakeFSM(),)),
        (f"grpplaceclear:{gid}:{ts}", cb_lesson_place_clear, (FakeFSM(),)),
    ):
        call = cb(data, 400, name="Чужой", message=SentMessage(bot, 400, ""))
        await handler(call, *extra)
        assert call.answers and call.answers[-1][1] is True  # show_alert block


# ── scheduler integration ────────────────────────────────────────────


async def test_scheduler_poll_uses_override_place(db, monkeypatch) -> None:
    """Опрос «придёшь?» показывает место занятия из override, а не площадку
    группы (единая точка lesson_place — её же используют напоминания,
    реактивация и инвайты саппортам)."""
    from app.services import admins as admins_svc
    from app.services import venues as venues_svc

    monday_lesson = datetime(2026, 5, 25, 19, 0, tzinfo=_MSK).astimezone(UTC)
    async with session_scope() as s:
        teacher = await admins_svc.get_or_create(s, 111, "t", "Препод")
        group = await groups_svc.create_group(
            s, teacher.id, "Группа A", "0", "19:00", 18
        )
        venue = await venues_svc.create_venue(s, group.school_id, "Студия Ритм")
        await groups_svc.update_group(s, group, venue_id=venue.id)
        await persons_svc.add_to_group(s, group, 200, "u0", "Ученик 0")
        gid = group.id
        await overrides_svc.set_place(s, gid, monday_lesson, "Тверская 10, зал 2")

    frozen = datetime(2026, 5, 25, 9, 0, tzinfo=_MSK).astimezone(UTC)
    monkeypatch.setattr(sched, "utc_now", lambda: frozen)

    class _Bot:
        def __init__(self):
            self.sent = []
            self._mid = 1000

        async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
            from types import SimpleNamespace
            self._mid += 1
            msg = SimpleNamespace(chat_id=chat_id, text=text, message_id=self._mid)
            self.sent.append(msg)
            return msg

    bot = _Bot()
    await sched.tick(bot)
    poll = [m for m in bot.sent if m.chat_id == 200]
    assert poll, [m.chat_id for m in bot.sent]
    assert "Тверская 10, зал 2" in poll[0].text
    assert "Студия Ритм" not in poll[0].text


async def test_reschedule_deletes_future_overrides(db) -> None:
    """Правка расписания с открытым опросом сносит будущие override'ы вместе
    с cancellation'ами — место по старому расписанию не «воскресает»."""
    gid, sid, code = await make_group()
    async with session_scope() as s:
        survey, _ = await surveys_svc.get_or_create_survey(
            s, gid, SurveyKind.attendance, LESSON
        )
        await overrides_svc.set_place(s, gid, LESSON, "зал 5")
        group = await groups_svc.get_group(s, gid)
        await groups_svc.update_group(s, group, lesson_time="20:15")
        assert await overrides_svc.get_place(s, gid, LESSON) is None
