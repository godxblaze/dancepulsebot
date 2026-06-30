"""E2E for the post-lesson attendance marking (attendance.py) and single-lesson
cancellation (lesson_cancel.py) flows."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.bot.handlers import lesson_cancel as lc
from app.bot.handlers.attendance import (
    cb_attendance_open_lesson,
    cb_attendance_page,
    cb_attendance_pick_lesson,
    cb_attendance_toggle,
    cb_support_attendance_toggle,
    cb_walk_in_add,
    cb_walk_in_page,
    cb_walk_in_pick,
)
from app.bot.handlers.lesson_cancel import (
    cb_lesson_cancel_ask_reason,
    cb_lesson_cancel_confirm,
    cb_lesson_cancel_pick,
    cb_lesson_cancel_skip_reason,
    lesson_cancel_reason_cancel,
    lesson_cancel_reason_typed,
)
from app.db.models import (
    Answer,
    Gender,
    Person,
    Response,
    SupportCallStatus,
    SurveyKind,
)
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import cancellations as cancellations_svc
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import schools as schools_svc
from app.services import supports as supports_svc
from app.services import surveys as surveys_svc
from app.services.schedule import upcoming_lessons
from tests._e2e_helpers import LESSON, TEACHER_TG, cb, join_via_link, make_group
from tests._fakes import FakeBot, FakeFSM, FakeMessage, SentMessage, fake_user

PAST = datetime(2026, 5, 4, 19, 0)  # an earlier Monday, naive UTC like the rest


async def _closed_survey_with_roster(gid: int, lesson_at=PAST) -> int:
    """Build a closed survey for `gid` with responses for its members. Returns survey_id."""
    async with session_scope() as s:
        survey, _ = await surveys_svc.get_or_create_survey(
            s, gid, SurveyKind.attendance, lesson_at
        )
        members = await persons_svc.group_members(s, gid)
        await surveys_svc.ensure_responses(s, survey.id, [p.id for p in members])
        for i, p in enumerate(members):
            await surveys_svc.record_answer(
                s, survey.id, p.id, Answer.yes if i % 2 == 0 else Answer.no
            )
        await surveys_svc.close_past_surveys(s, lesson_at + timedelta(days=1))
        return survey.id


# ── attendance marking ───────────────────────────────────────────────


async def test_attendance_marking_flow(db) -> None:
    bot = FakeBot()
    gid, sid, code = await make_group()
    for tg, name, g in (
        (501, "Аня", Gender.female),
        (502, "Боря", Gender.male),
        (503, "Вика", Gender.female),
    ):
        await join_via_link(bot, code, tg, name, g)
    survey_id = await _closed_survey_with_roster(gid)

    # 1. pick lesson — closed surveys list
    pick_msg = SentMessage(bot, TEACHER_TG, "")
    await cb_attendance_pick_lesson(cb(f"grpattnmark:{gid}", TEACHER_TG, message=pick_msg))
    assert "отметить" in pick_msg.text

    # 2. open the roster
    roster = SentMessage(bot, TEACHER_TG, "")
    await cb_attendance_open_lesson(cb(f"atnlesson:{survey_id}:{gid}", TEACHER_TG, message=roster))
    assert "Тап по имени" in roster.text

    # 3. toggle one person None → True → False, then a stale tap
    async with session_scope() as s:
        members = await persons_svc.group_members(s, gid)
        pid = members[0].id

    await cb_attendance_toggle(cb(f"atntog:{survey_id}:{pid}:x:0", TEACHER_TG, message=roster))
    async with session_scope() as s:
        att = (await s.execute(
            Response.__table__.select().where(
                (Response.survey_id == survey_id) & (Response.person_id == pid)
            )
        )).first()
        assert att.actually_attended is True

    await cb_attendance_toggle(cb(f"atntog:{survey_id}:{pid}:y:0", TEACHER_TG, message=roster))
    async with session_scope() as s:
        att = (await s.execute(
            Response.__table__.select().where(
                (Response.survey_id == survey_id) & (Response.person_id == pid)
            )
        )).first()
        assert att.actually_attended is False

    # stale tap — expected 'x' but current is False → "Уже обновлено", no change
    stale = cb(f"atntog:{survey_id}:{pid}:x:0", TEACHER_TG, message=roster)
    await cb_attendance_toggle(stale)
    assert any("Уже обновлено" in a[0] for a in stale.answers)

    # 4. pagination handler (single page — still renders)
    await cb_attendance_page(cb(f"atnpg:{survey_id}:{gid}:0", TEACHER_TG, message=roster))


async def test_attendance_walk_in(db) -> None:
    bot = FakeBot()
    gid, sid, code = await make_group()
    for tg, name, g in ((601, "Аня", Gender.female), (602, "Боря", Gender.male)):
        await join_via_link(bot, code, tg, name, g)
    survey_id = await _closed_survey_with_roster(gid)
    # a person in the school NOT in this survey's roster — joins after responses built
    await join_via_link(bot, code, 603, "Гость", Gender.male)

    roster = SentMessage(bot, TEACHER_TG, "")
    await cb_walk_in_pick(cb(f"atnwk:{survey_id}:{gid}", TEACHER_TG, message=roster))
    assert "walk-in" in roster.text or "Кого добавить" in roster.text

    # page handler
    await cb_walk_in_page(cb(f"wkpg:{survey_id}:{gid}:0", TEACHER_TG, message=roster))

    async with session_scope() as s:
        guest = await persons_svc.get_person(s, sid, 603)
        guest_id = guest.id

    add = cb(f"atnwkpick:{survey_id}:{guest_id}", TEACHER_TG, message=roster)
    await cb_walk_in_add(add)
    assert any("гост" in a[0].lower() for a in add.answers)
    async with session_scope() as s:
        roster_ids = {p.id for p, _ in await surveys_svc.attendance_roster(s, survey_id)}
        assert guest_id in roster_ids


async def test_support_attendance_toggle(db) -> None:
    bot = FakeBot()
    gid, sid, code = await make_group()
    await join_via_link(bot, code, 701, "Аня", Gender.female)
    survey_id = await _closed_survey_with_roster(gid)

    # a support person + an open call + their claim
    async with session_scope() as s:
        group = await groups_svc.get_group(s, gid)
        sup, _ = await persons_svc.add_to_group(s, group, 710, "u", "Витя")
        await persons_svc.set_gender(s, sup.id, Gender.male)
        call = await supports_svc.create_call(
            s, survey_id, Gender.male, 1, SupportCallStatus.open
        )
        await supports_svc.claim(s, call.id, sup.id)
        sup_id = sup.id
    async with session_scope() as s:
        rows = await supports_svc.claimed_supports_for_survey(s, survey_id)
        claim_id = rows[0][1].id

    roster = SentMessage(bot, TEACHER_TG, "")
    # ☐ → ✅ marks them present → helped_count becomes 1
    await cb_support_attendance_toggle(
        cb(f"supatt:{survey_id}:{claim_id}:x:0", TEACHER_TG, message=roster)
    )
    async with session_scope() as s:
        assert (await s.get(Person, sup_id)).helped_count == 1


# ── lesson cancellation ──────────────────────────────────────────────


def _pin_now(monkeypatch, now):
    monkeypatch.setattr(lc, "utc_now", lambda: now)


async def _first_upcoming(gid: int, now) -> datetime:
    async with session_scope() as s:
        from app.services import groups as groups_svc
        group = await groups_svc.get_group(s, gid)
        tz = await schools_svc.get_timezone(s, group.school_id)
        ups = upcoming_lessons(group.weekdays, group.lesson_time, now, n=5, tz=tz)
    assert ups
    return ups[0]


async def test_cancel_lesson_with_reason_and_notices(db, monkeypatch) -> None:
    bot = FakeBot()
    gid, sid, code = await make_group()
    now = LESSON - timedelta(hours=2)
    _pin_now(monkeypatch, now)
    dt = await _first_upcoming(gid, now)

    # a yes-student so they get a cancel notice
    await join_via_link(bot, code, 801, "Аня", Gender.female)
    async with session_scope() as s:
        survey, _ = await surveys_svc.get_or_create_survey(s, gid, SurveyKind.attendance, dt)
        members = await persons_svc.group_members(s, gid)
        await surveys_svc.ensure_responses(s, survey.id, [p.id for p in members])
        for p in members:
            await surveys_svc.record_answer(s, survey.id, p.id, Answer.yes)
        # a support who claimed → should get a "помощь не нужна" notice
        group = await groups_svc.get_group(s, gid)
        sup, _ = await persons_svc.add_to_group(s, group, 810, "u", "Слава")
        await persons_svc.set_gender(s, sup.id, Gender.male)
        # needed=2 so a single claim keeps the call open → cancel notifies the support
        call = await supports_svc.create_call(s, survey.id, Gender.male, 2, SupportCallStatus.open)
        await supports_svc.claim(s, call.id, sup.id)
        survey_id = survey.id
        call_id = call.id
    # a co-admin so the coadmin-notice branch fires
    async with session_scope() as s:
        co = await admins_svc.get_or_create(s, 820, "co", "Соадмин")
        await schools_svc.add_coadmin(s, sid, co.id)

    ts = int(dt.timestamp())

    # pick → confirm → ask-reason → typed reason → execute
    pick = SentMessage(bot, TEACHER_TG, "")
    await cb_lesson_cancel_pick(cb(f"grpcancel:{gid}", TEACHER_TG, message=pick))
    assert "Отменить какое занятие" in pick.text

    await cb_lesson_cancel_confirm(cb(f"grpcanceldt:{gid}:{ts}", TEACHER_TG, message=pick))
    assert "Отменить занятие" in pick.text

    state = FakeFSM()
    await cb_lesson_cancel_ask_reason(
        cb(f"grpcancelok:{gid}:{ts}", TEACHER_TG, message=pick), state
    )
    assert state.state == lc.CancelLesson.reason

    reason_msg = FakeMessage(fake_user(TEACHER_TG), text="Зал занят", bot=bot)
    await lesson_cancel_reason_typed(reason_msg, state, bot)
    assert reason_msg.replies and "отменено" in reason_msg.replies[0].text

    async with session_scope() as s:
        assert await cancellations_svc.is_cancelled(s, gid, dt)
        assert (await supports_svc.get_call(s, call_id)).status == SupportCallStatus.closed
        assert (await surveys_svc.get_survey(s, survey_id)).status.value == "closed"

    # student, support and co-admin each got a notice with the reason
    assert any("отменено" in m.text and "Зал занят" in m.text for m in bot.sent_to(801))
    assert any("больше не нужна" in m.text for m in bot.sent_to(810))
    assert any("отменил другой админ" in m.text for m in bot.sent_to(820))


async def test_cancel_lesson_skip_reason(db, monkeypatch) -> None:
    bot = FakeBot()
    gid, sid, code = await make_group()
    now = LESSON - timedelta(hours=2)
    _pin_now(monkeypatch, now)
    dt = await _first_upcoming(gid, now)
    ts = int(dt.timestamp())

    pick = SentMessage(bot, TEACHER_TG, "")
    await cb_lesson_cancel_confirm(cb(f"grpcanceldt:{gid}:{ts}", TEACHER_TG, message=pick))
    state = FakeFSM()
    await cb_lesson_cancel_ask_reason(
        cb(f"grpcancelok:{gid}:{ts}", TEACHER_TG, message=pick), state
    )
    await cb_lesson_cancel_skip_reason(
        cb("cnclreason:skip", TEACHER_TG, message=pick), state, bot
    )
    assert "отменено" in pick.text
    async with session_scope() as s:
        assert await cancellations_svc.is_cancelled(s, gid, dt)

    # confirming an already-cancelled lesson → alert, no double-cancel
    again = cb(f"grpcanceldt:{gid}:{ts}", TEACHER_TG, message=SentMessage(bot, TEACHER_TG, ""))
    await cb_lesson_cancel_confirm(again)
    assert any("уже отменено" in a[0] for a in again.answers)


async def test_cancel_lesson_aborted(db, monkeypatch) -> None:
    bot = FakeBot()
    gid, sid, code = await make_group()
    now = LESSON - timedelta(hours=2)
    _pin_now(monkeypatch, now)
    state = FakeFSM()
    await state.set_state(lc.CancelLesson.reason)
    msg = FakeMessage(fake_user(TEACHER_TG), text="/cancel", bot=bot)
    await lesson_cancel_reason_cancel(msg, state)
    assert state.state is None
    assert msg.replies and "ничего не изменилось" in msg.replies[0].text
