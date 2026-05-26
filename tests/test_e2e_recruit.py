"""End-to-end scenario — every Telegram tap from group creation to a support
saying yes, driven through the real handlers and the real SQLite, with no bot.

If this passes, the recruit flow works without a single human click in TG.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.bot.handlers.admin import (
    cb_pool_toggle,
    newgroup_level_pick,
    newgroup_room_skip,
    newgroup_start,
    newgroup_style_pick,
    newgroup_style_typed,
    newgroup_teachers_skip,
    newgroup_time,
    newgroup_venue_pick,
    newgroup_weekdays,
)
from app.bot.handlers.recruit import (
    cb_support_join,
    cb_support_pick,
    cb_support_pick_send,
)
from app.bot.handlers.student import join_gender, join_name_typed, join_start
from app.bot.handlers.survey import on_attendance
from app.bot.states import JoinFlow, NewGroup
from app.db.models import Gender, Person, SupportCallStatus
from app.db.session import session_scope
from app.scheduler.scheduler import _maybe_recruit, _poll_group
from app.services import admins as admins_svc
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import supports as supports_svc
from app.services import surveys as surveys_svc
from tests._fakes import (
    FakeBot,
    FakeCallback,
    FakeFSM,
    FakeMessage,
    SentMessage,
    fake_user,
)

LESSON = datetime(2026, 6, 1, 19, 0)  # Monday 19:00
TEACHER_TG = 100


# ── helpers ──────────────────────────────────────────────────────────


async def _create_group_via_wizard(bot: FakeBot, monkeypatch) -> int:
    """Drive /newgroup → Mon → 19:00 entirely through handlers. Returns group_id."""

    async def _fake_username(_bot) -> str:
        return "dancepulsebot"

    monkeypatch.setattr("app.bot.handlers.admin.get_bot_username", _fake_username)
    teacher = fake_user(TEACHER_TG, full_name="Препод")
    fsm = FakeFSM()

    async with session_scope() as s:
        await admins_svc.get_or_create(s, TEACHER_TG, None, "Препод")

    start_msg = FakeMessage(teacher, bot=bot)
    await newgroup_start(start_msg, fsm)
    assert fsm.state == NewGroup.style

    # pick «✏️ Ввести своё» then type the style name
    style_kb_msg = start_msg.replies[-1]
    await newgroup_style_pick(
        FakeCallback("ngst:type", teacher, message=style_kb_msg), fsm
    )
    assert fsm.state == NewGroup.style_typing
    style_typed_msg = FakeMessage(teacher, text="Бачата", bot=bot)
    await newgroup_style_typed(style_typed_msg, fsm)
    assert fsm.state == NewGroup.level

    level_kb_msg = style_typed_msg.replies[-1]
    await newgroup_level_pick(
        FakeCallback("nglv:skip", teacher, message=level_kb_msg), fsm
    )
    assert fsm.state == NewGroup.weekdays

    wd_kb_msg = level_kb_msg.replies[-1]
    await newgroup_weekdays(FakeCallback("wd:0", teacher, message=wd_kb_msg), fsm)
    await newgroup_weekdays(FakeCallback("wd:done", teacher, message=wd_kb_msg), fsm)
    assert fsm.state == NewGroup.lesson_time

    time_msg = FakeMessage(teacher, text="19:00", bot=bot)
    await newgroup_time(time_msg, fsm, bot)
    assert fsm.state == NewGroup.venue

    venue_kb_msg = time_msg.replies[-1]
    await newgroup_venue_pick(
        FakeCallback("ngvn:skip", teacher, message=venue_kb_msg), fsm
    )
    assert fsm.state == NewGroup.room
    room_kb_msg = venue_kb_msg.replies[-1]
    await newgroup_room_skip(
        FakeCallback("ngrm:skip", teacher, message=room_kb_msg), fsm
    )
    assert fsm.state == NewGroup.teachers_note
    teachers_kb_msg = room_kb_msg.replies[-1]
    await newgroup_teachers_skip(
        FakeCallback("ngtn:skip", teacher, message=teachers_kb_msg), fsm, bot
    )
    assert fsm.state is None

    async with session_scope() as s:
        t = await admins_svc.get_by_tg_id(s, TEACHER_TG)
        assert t is not None
        gs = await groups_svc.admin_visible_groups(s, t.id)
        assert len(gs) == 1 and gs[0].weekdays == "0" and gs[0].lesson_time == "19:00"
        return gs[0].id


async def _join_via_link(
    bot: FakeBot, code: str, tg_id: int, name: str, gender: Gender
) -> None:
    """Drive the join FSM: deep-link → typed name → gender pick."""
    user = fake_user(tg_id, full_name=name)
    fsm = FakeFSM()
    start_msg = FakeMessage(user, bot=bot)
    await join_start(start_msg, SimpleNamespace(args=f"join_{code}"), fsm, bot)
    assert fsm.state == JoinFlow.name

    await join_name_typed(FakeMessage(user, text=name, bot=bot), fsm)
    assert fsm.state == JoinFlow.gender

    gender_cb = FakeCallback(
        f"join:gender:{'male' if gender == Gender.male else 'female'}",
        user,
        message=SentMessage(None, tg_id, ""),
    )
    await join_gender(gender_cb, fsm, bot)
    assert fsm.state is None


# ── the scenario ─────────────────────────────────────────────────────


async def test_full_recruit_scenario(db, monkeypatch) -> None:
    bot = FakeBot()

    # 1. Teacher creates a group via the /newgroup wizard.
    group_id = await _create_group_via_wizard(bot, monkeypatch)
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        assert group is not None
        join_code = group.join_code
        admin_id = group.school_id

    # 2. Six students onboard through the join link: 1 male + 5 female (deficit 4).
    student_specs = [
        (501, "Боря", Gender.male),
        (502, "Аня", Gender.female),
        (503, "Маша", Gender.female),
        (504, "Лена", Gender.female),
        (505, "Оля", Gender.female),
        (506, "Катя", Gender.female),
    ]
    for tg_id, name, gender in student_specs:
        await _join_via_link(bot, join_code, tg_id, name, gender)
    # the teacher got a notification per join
    assert sum(1 for m in bot.sent_to(TEACHER_TG) if "записал" in m.text.lower()) == 6

    # 3. Scheduler tick — poll the group. Real SQLite, real handlers, fake bot.
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
    await _poll_group(bot, group, LESSON)
    async with session_scope() as s:
        survey = await surveys_svc.find_attendance_survey(s, group_id, LESSON)
        assert survey is not None
        survey_id = survey.id

    # 4. Every student taps "Приду" on the poll message addressed to them.
    for tg_id, name, _ in student_specs:
        poll_msg = bot.last_to(tg_id)
        assert "Придёшь" in poll_msg.text
        cb = FakeCallback(
            f"att:{survey_id}:yes", fake_user(tg_id, full_name=name), message=poll_msg
        )
        await on_attendance(cb)
        assert "придёшь" in poll_msg.text.lower()  # message was edited

    # 5. Two male supports exist in a sibling group; teacher promotes both to
    #    her personal pool via /pool (one tap = level 1).
    async with session_scope() as s:
        sibling = await groups_svc.create_group(s, admin_id, "Другая", "2", "20:00", 18)
        sup1, _ = await persons_svc.add_to_group(s, sibling, 701, "u", "Витя")
        await persons_svc.set_gender(s, sup1.id, Gender.male)
        sup2, _ = await persons_svc.add_to_group(s, sibling, 702, "u", "Слава")
        await persons_svc.set_gender(s, sup2.id, Gender.male)
        sup1_id, sup2_id = sup1.id, sup2.id

    teacher = fake_user(TEACHER_TG, full_name="Препод")
    for pid in (sup1_id, sup2_id):
        await cb_pool_toggle(
            FakeCallback(f"tpool:{pid}", teacher, message=SentMessage(None, TEACHER_TG, ""))
        )
    async with session_scope() as s:
        assert (await s.get(Person, sup1_id)).support_level == 1
        assert (await s.get(Person, sup2_id)).support_level == 1

    # 6. Cutoff: scheduler measures the deficit and pings the teacher with the picker.
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        t = await admins_svc.get_by_tg_id(s, TEACHER_TG)
        assert t is not None
        cutoff_h = t.support_cutoff_hours
    await _maybe_recruit(bot, group, LESSON, LESSON - timedelta(hours=cutoff_h - 1))

    async with session_scope() as s:
        call = await supports_svc.get_call_for_survey(s, survey_id)
        assert call is not None
        assert call.status == SupportCallStatus.pending
        assert call.gender == Gender.male and call.needed == 4
        call_id = call.id

    picker = bot.last_to(TEACHER_TG)
    assert "не хватает" in picker.text
    # both pool members are offered as buttons
    btn_callbacks = [
        b.callback_data for row in picker.reply_markup.inline_keyboard for b in row
    ]
    assert f"pick:{call_id}:{sup1_id}" in btn_callbacks
    assert f"pick:{call_id}:{sup2_id}" in btn_callbacks

    # 7. Teacher ticks both, then taps "Позвать".
    await cb_support_pick(
        FakeCallback(f"pick:{call_id}:{sup1_id}", teacher, message=picker)
    )
    await cb_support_pick(
        FakeCallback(f"pick:{call_id}:{sup2_id}", teacher, message=picker)
    )
    await cb_support_pick_send(
        FakeCallback(f"picksend:{call_id}", teacher, message=picker), bot
    )

    # invitations went out, call moved to open
    for tg_id in (701, 702):
        invite = bot.last_to(tg_id)
        assert "Нужна помощь" in invite.text
    async with session_scope() as s:
        assert (await supports_svc.get_call(s, call_id)).status == SupportCallStatus.open

    # 8. Both supports tap "Приду помочь" — needed=4, only 2 claims, stays open.
    for sup_tg in (701, 702):
        invite = bot.last_to(sup_tg)
        await cb_support_join(
            FakeCallback(
                f"sup:join:{call_id}",
                fake_user(sup_tg, full_name="Саппорт"),
                message=invite,
            ),
            bot,
        )

    async with session_scope() as s:
        sc = await supports_svc.get_call(s, call_id)
        assert sc.status == SupportCallStatus.open  # 2/4 — still open
        assert await supports_svc.count_claims(s, call_id) == 2
        assert (await s.get(Person, sup1_id)).helped_count == 1
        assert (await s.get(Person, sup2_id)).helped_count == 1

    # teacher got the "filled X/Y" pings
    pings = [m for m in bot.sent_to(TEACHER_TG) if "придёт выручить" in m.text]
    assert len(pings) == 2 and "2/4" in pings[-1].text
