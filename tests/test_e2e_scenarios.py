"""End-to-end scenarios for Dance Pulse — every interaction goes through real
handlers and the real SQLite. Each test drives one user-visible flow."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.bot.handlers.admin import (
    cb_group_delete,
    cb_group_delete_confirm,
    cb_pool_toggle,
    cmd_grant,
    cmd_stats,
    cmd_today,
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
from app.bot.handlers.student import (
    join_gender,
    join_name_confirm,
    join_name_typed,
    join_start,
)
from app.bot.handlers.survey import on_attendance
from app.bot.states import JoinFlow, NewGroup
from app.core.config import settings
from app.db.models import Answer, Gender, Person, SupportCallStatus, SurveyKind
from app.db.session import session_scope
from app.scheduler import scheduler as sched_module
from app.scheduler.scheduler import (
    _maybe_recruit,
    _maybe_send_summary,
    _poll_group,
)
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


async def _create_group_via_wizard(
    bot: FakeBot,
    monkeypatch,
    *,
    style: str = "Бачата",
    weekday: int = 0,
    time: str = "19:00",
    teacher_tg: int = TEACHER_TG,
    teacher_name: str = "Препод",
) -> int:
    async def _fake_username(_bot) -> str:
        return "dancepulsebot"

    monkeypatch.setattr("app.bot.handlers.admin.get_bot_username", _fake_username)
    teacher = fake_user(teacher_tg, full_name=teacher_name)
    fsm = FakeFSM()

    async with session_scope() as s:
        await admins_svc.get_or_create(s, teacher_tg, None, teacher_name)

    start_msg = FakeMessage(teacher, bot=bot)
    await newgroup_start(start_msg, fsm)
    assert fsm.state == NewGroup.style

    # type custom style
    style_kb_msg = start_msg.replies[-1]
    await newgroup_style_pick(
        FakeCallback("ngst:type", teacher, message=style_kb_msg), fsm
    )
    assert fsm.state == NewGroup.style_typing
    style_typed_msg = FakeMessage(teacher, text=style, bot=bot)
    await newgroup_style_typed(style_typed_msg, fsm)
    assert fsm.state == NewGroup.level

    level_kb_msg = style_typed_msg.replies[-1]
    await newgroup_level_pick(
        FakeCallback("nglv:skip", teacher, message=level_kb_msg), fsm
    )
    assert fsm.state == NewGroup.weekdays

    wd_kb_msg = level_kb_msg.replies[-1]
    await newgroup_weekdays(
        FakeCallback(f"wd:{weekday}", teacher, message=wd_kb_msg), fsm
    )
    await newgroup_weekdays(FakeCallback("wd:done", teacher, message=wd_kb_msg), fsm)
    assert fsm.state == NewGroup.lesson_time

    time_msg = FakeMessage(teacher, text=time, bot=bot)
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
        t = await admins_svc.get_by_tg_id(s, teacher_tg)
        assert t is not None
        gs = await groups_svc.admin_groups(s, t.id)
        # newest group is the last one
        return gs[-1].id


async def _join_via_link(
    bot: FakeBot, code: str, tg_id: int, name: str, gender: Gender
) -> None:
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


# ── 1. group lifecycle ───────────────────────────────────────────────


async def test_group_lifecycle(db, monkeypatch) -> None:
    bot = FakeBot()
    teacher = fake_user(TEACHER_TG, full_name="Препод")

    g1_id = await _create_group_via_wizard(bot, monkeypatch, style="Бачата", weekday=0)
    g2_id = await _create_group_via_wizard(bot, monkeypatch, style="Сальса", weekday=2)
    assert g1_id != g2_id

    async with session_scope() as s:
        g1 = await groups_svc.get_group(s, g1_id)
        deleted_code = g1.join_code

    # Confirm dialog → actual delete
    await cb_group_delete_confirm(
        FakeCallback(f"grpdel:{g1_id}", teacher, message=SentMessage(None, TEACHER_TG, ""))
    )
    await cb_group_delete(
        FakeCallback(f"grpdelok:{g1_id}", teacher, message=SentMessage(None, TEACHER_TG, ""))
    )

    async with session_scope() as s:
        assert await groups_svc.get_group(s, g1_id) is None
        t = await admins_svc.get_by_tg_id(s, TEACHER_TG)
        gs = await groups_svc.admin_groups(s, t.id)
        assert len(gs) == 1 and gs[0].id == g2_id
        live_code = gs[0].join_code

    # student tries deleted group's code
    student = fake_user(500, full_name="Учн")
    fsm = FakeFSM()
    dead_msg = FakeMessage(student, bot=bot)
    await join_start(dead_msg, SimpleNamespace(args=f"join_{deleted_code}"), fsm, bot)
    assert any("не найдена" in r.text for r in dead_msg.replies)
    assert fsm.state is None

    # ...then the live one — normal onboarding starts
    fsm2 = FakeFSM()
    live_msg = FakeMessage(student, bot=bot)
    await join_start(live_msg, SimpleNamespace(args=f"join_{live_code}"), fsm2, bot)
    assert fsm2.state == JoinFlow.name


# ── 2. onboarding: suggested name button ─────────────────────────────


async def test_onboarding_suggested_name(db, monkeypatch) -> None:
    bot = FakeBot()
    gid = await _create_group_via_wizard(bot, monkeypatch)
    async with session_scope() as s:
        g = await groups_svc.get_group(s, gid)
        code = g.join_code
        tid = g.admin_id

    student = fake_user(501, full_name="Аня Иванова", username="anya")
    fsm = FakeFSM()
    start_msg = FakeMessage(student, bot=bot)
    await join_start(start_msg, SimpleNamespace(args=f"join_{code}"), fsm, bot)
    assert fsm.state == JoinFlow.name

    # Tap the "use suggested name" inline button
    name_cb = FakeCallback(
        "join:name_ok", student, message=SentMessage(None, student.id, "")
    )
    await join_name_confirm(name_cb, fsm)
    assert fsm.state == JoinFlow.gender

    gender_cb = FakeCallback(
        "join:gender:female", student, message=SentMessage(None, student.id, "")
    )
    await join_gender(gender_cb, fsm, bot)
    assert fsm.state is None

    async with session_scope() as s:
        p = await persons_svc.get_person(s, tid, 501)
        assert p is not None
        # full_name was taken from telegram, not typed
        assert p.full_name == "Аня Иванова"
        assert p.gender == Gender.female


# ── 3. returning person fast-path ────────────────────────────────────


async def test_returning_person_fast_path(db, monkeypatch) -> None:
    bot = FakeBot()
    g1_id = await _create_group_via_wizard(bot, monkeypatch, style="G1", weekday=0)
    g2_id = await _create_group_via_wizard(bot, monkeypatch, style="G2", weekday=2)

    async with session_scope() as s:
        g1 = await groups_svc.get_group(s, g1_id)
        g2 = await groups_svc.get_group(s, g2_id)
        c1, c2 = g1.join_code, g2.join_code

    # full onboarding to g1
    await _join_via_link(bot, c1, 600, "Аня", Gender.female)
    teacher_msgs_before = len([m for m in bot.sent_to(TEACHER_TG) if "записал" in m.text.lower()])

    # to g2 — fast path: no FSM, no onboarding
    student = fake_user(600, full_name="Аня")
    fsm = FakeFSM()
    start_msg = FakeMessage(student, bot=bot)
    await join_start(start_msg, SimpleNamespace(args=f"join_{c2}"), fsm, bot)
    assert fsm.state is None
    assert any("Записал тебя" in r.text for r in start_msg.replies)

    async with session_scope() as s:
        assert await persons_svc.count_members(s, g1_id) == 1
        assert await persons_svc.count_members(s, g2_id) == 1
    teacher_msgs_after = len(
        [m for m in bot.sent_to(TEACHER_TG) if "записал" in m.text.lower()]
    )
    assert teacher_msgs_after == teacher_msgs_before + 1
    assert teacher_msgs_after == 2  # one per group


# ── 4. newgroup wizard validation ────────────────────────────────────


async def test_newgroup_wizard_validation(db, monkeypatch) -> None:
    async def _fake_username(_bot) -> str:
        return "dancepulsebot"

    monkeypatch.setattr("app.bot.handlers.admin.get_bot_username", _fake_username)
    bot = FakeBot()
    teacher = fake_user(TEACHER_TG, full_name="Препод")
    fsm = FakeFSM()

    async with session_scope() as s:
        await admins_svc.get_or_create(s, TEACHER_TG, None, "Препод")

    start_msg = FakeMessage(teacher, bot=bot)
    await newgroup_start(start_msg, fsm)
    assert fsm.state == NewGroup.style

    style_msg = start_msg.replies[-1]
    await newgroup_style_pick(
        FakeCallback("ngst:skip", teacher, message=style_msg), fsm
    )
    assert fsm.state == NewGroup.level

    level_msg = style_msg.replies[-1]
    await newgroup_level_pick(
        FakeCallback("nglv:skip", teacher, message=level_msg), fsm
    )
    assert fsm.state == NewGroup.weekdays

    wd_msg = level_msg.replies[-1]

    # press Done with no weekdays picked → alert, FSM stays at weekdays
    done_cb = FakeCallback("wd:done", teacher, message=wd_msg)
    await newgroup_weekdays(done_cb, fsm)
    assert fsm.state == NewGroup.weekdays
    assert any(show_alert and "хотя бы один" in t for t, show_alert in done_cb.answers)

    # pick Monday, then Done — state advances
    await newgroup_weekdays(FakeCallback("wd:0", teacher, message=wd_msg), fsm)
    await newgroup_weekdays(FakeCallback("wd:done", teacher, message=wd_msg), fsm)
    assert fsm.state == NewGroup.lesson_time

    # malformed time → handler complains, FSM unchanged
    bad_time = FakeMessage(teacher, text="abc", bot=bot)
    await newgroup_time(bad_time, fsm, bot)
    assert fsm.state == NewGroup.lesson_time
    assert any("Не похоже на время" in r.text for r in bad_time.replies)

    # good time — wizard advances to venue picker
    good_time = FakeMessage(teacher, text="19:00", bot=bot)
    await newgroup_time(good_time, fsm, bot)
    assert fsm.state == NewGroup.venue

    venue_msg = good_time.replies[-1]
    await newgroup_venue_pick(
        FakeCallback("ngvn:skip", teacher, message=venue_msg), fsm
    )
    assert fsm.state == NewGroup.room
    room_msg = venue_msg.replies[-1]
    await newgroup_room_skip(
        FakeCallback("ngrm:skip", teacher, message=room_msg), fsm
    )
    assert fsm.state == NewGroup.teachers_note
    teachers_msg = room_msg.replies[-1]
    await newgroup_teachers_skip(
        FakeCallback("ngtn:skip", teacher, message=teachers_msg), fsm, bot
    )
    assert fsm.state is None

    async with session_scope() as s:
        t = await admins_svc.get_by_tg_id(s, TEACHER_TG)
        assert t is not None
        gs = await groups_svc.admin_groups(s, t.id)
        assert len(gs) == 1
        assert gs[0].weekdays == "0" and gs[0].lesson_time == "19:00"


# ── 5. poll: mixed yes/no/silent ─────────────────────────────────────


async def test_poll_mixed_answers(db, monkeypatch) -> None:
    bot = FakeBot()
    group_id = await _create_group_via_wizard(bot, monkeypatch)
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        code = group.join_code

    specs = [
        (501, "Аня", Gender.female),
        (502, "Боря", Gender.male),
        (503, "Вика", Gender.female),
        (504, "Гена", Gender.male),
        (505, "Дима", Gender.male),
    ]
    for tg, name, gender in specs:
        await _join_via_link(bot, code, tg, name, gender)

    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
    await _poll_group(bot, group, LESSON)
    async with session_scope() as s:
        survey = await surveys_svc.find_attendance_survey(s, group_id, LESSON)
        survey_id = survey.id

    # 2 yes, 2 no, 1 silent (Дима)
    yes_specs = specs[:2]
    no_specs = specs[2:4]
    silent_spec = specs[4]

    for tg, name, _ in yes_specs:
        poll_msg = bot.last_to(tg)
        await on_attendance(
            FakeCallback(
                f"att:{survey_id}:yes", fake_user(tg, full_name=name), message=poll_msg
            )
        )
        assert "придёшь" in poll_msg.text.lower()

    for tg, name, _ in no_specs:
        poll_msg = bot.last_to(tg)
        await on_attendance(
            FakeCallback(
                f"att:{survey_id}:no", fake_user(tg, full_name=name), message=poll_msg
            )
        )
        assert "не придёшь" in poll_msg.text.lower()

    # silent: poll message stays untouched (still has "Придёшь?")
    silent_msg = bot.last_to(silent_spec[0])
    assert "Придёшь" in silent_msg.text

    # /today — pin clock to "today is Monday, lesson today"
    monkeypatch.setattr(
        "app.bot.handlers.admin.local_now",
        lambda: LESSON - timedelta(hours=4),
    )
    today_msg = FakeMessage(fake_user(TEACHER_TG, full_name="Препод"), bot=bot)
    await cmd_today(today_msg)
    assert today_msg.replies
    text = today_msg.replies[0].text
    assert "2/5" in text
    assert silent_spec[1] in text  # the silent person is listed under ⏳


# ── 6. auto-summary is idempotent ────────────────────────────────────


async def test_auto_summary_idempotent(db, monkeypatch) -> None:
    bot = FakeBot()
    group_id = await _create_group_via_wizard(bot, monkeypatch)
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        code = group.join_code

    for tg, name, gender in (
        (601, "Аня", Gender.female),
        (602, "Боря", Gender.male),
    ):
        await _join_via_link(bot, code, tg, name, gender)

    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
    await _poll_group(bot, group, LESSON)

    # halfway through lead window
    now = LESSON - timedelta(hours=group.lead_hours / 2)

    teacher_msgs_before = [
        m for m in bot.sent_to(TEACHER_TG) if "итоги опроса" in m.text
    ]
    assert teacher_msgs_before == []

    await _maybe_send_summary(bot, group, LESSON, now)
    after_first = [m for m in bot.sent_to(TEACHER_TG) if "итоги опроса" in m.text]
    assert len(after_first) == 1

    # second call — must NOT send a duplicate
    await _maybe_send_summary(bot, group, LESSON, now)
    after_second = [m for m in bot.sent_to(TEACHER_TG) if "итоги опроса" in m.text]
    assert len(after_second) == 1


# ── 7. recruit: deficit below threshold ──────────────────────────────


async def test_recruit_below_threshold(db, monkeypatch) -> None:
    bot = FakeBot()
    group_id = await _create_group_via_wizard(bot, monkeypatch)
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        code = group.join_code

    # 2 male yes vs 3 female yes → deficit 1, default threshold = 2
    specs = [
        (701, "M1", Gender.male),
        (702, "M2", Gender.male),
        (703, "F1", Gender.female),
        (704, "F2", Gender.female),
        (705, "F3", Gender.female),
    ]
    for tg, name, gender in specs:
        await _join_via_link(bot, code, tg, name, gender)

    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
    await _poll_group(bot, group, LESSON)
    async with session_scope() as s:
        survey = await surveys_svc.find_attendance_survey(s, group_id, LESSON)
        survey_id = survey.id

    for tg, name, _ in specs:
        msg = bot.last_to(tg)
        await on_attendance(
            FakeCallback(
                f"att:{survey_id}:yes", fake_user(tg, full_name=name), message=msg
            )
        )

    sent_before = len(bot.sent_to(TEACHER_TG))
    await _maybe_recruit(bot, group, LESSON, LESSON - timedelta(hours=3))

    async with session_scope() as s:
        call = await supports_svc.get_call_for_survey(s, survey_id)
        assert call is not None
        assert call.status == SupportCallStatus.closed
        assert call.needed == 0

    # teacher got no "не хватает" message
    new_msgs = bot.sent_to(TEACHER_TG)[sent_before:]
    assert not any("не хватает" in m.text for m in new_msgs)


# ── 8. recruit: empty pool ───────────────────────────────────────────


async def test_recruit_empty_pool(db, monkeypatch) -> None:
    bot = FakeBot()
    group_id = await _create_group_via_wizard(bot, monkeypatch)
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        code = group.join_code

    # one male + 4 females → deficit 3, no pool members exist anywhere
    specs = [
        (801, "M1", Gender.male),
        (802, "F1", Gender.female),
        (803, "F2", Gender.female),
        (804, "F3", Gender.female),
        (805, "F4", Gender.female),
    ]
    for tg, name, gender in specs:
        await _join_via_link(bot, code, tg, name, gender)

    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
    await _poll_group(bot, group, LESSON)
    async with session_scope() as s:
        survey = await surveys_svc.find_attendance_survey(s, group_id, LESSON)
        survey_id = survey.id
    for tg, name, _ in specs:
        msg = bot.last_to(tg)
        await on_attendance(
            FakeCallback(
                f"att:{survey_id}:yes", fake_user(tg, full_name=name), message=msg
            )
        )

    await _maybe_recruit(bot, group, LESSON, LESSON - timedelta(hours=3))

    picker_candidates = [
        m for m in bot.sent_to(TEACHER_TG) if "не хватает" in m.text
    ]
    assert len(picker_candidates) == 1
    msg = picker_candidates[0]
    assert "В пуле нет подходящих" in msg.text
    # no inline pick: keyboard
    if msg.reply_markup is None:
        pick_btns = []
    else:
        pick_btns = [
            b.callback_data
            for row in msg.reply_markup.inline_keyboard
            for b in row
            if (b.callback_data or "").startswith("pick:")
        ]
    assert pick_btns == []


# ── 9. recruit: foreign teacher cannot send / toggle ─────────────────


async def test_recruit_foreign_teacher_blocked(db, monkeypatch) -> None:
    bot = FakeBot()
    group_id = await _create_group_via_wizard(bot, monkeypatch)
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        code = group.join_code
        admin_id = group.admin_id

    specs = [
        (901, "M1", Gender.male),
        (902, "F1", Gender.female),
        (903, "F2", Gender.female),
        (904, "F3", Gender.female),
        (905, "F4", Gender.female),
    ]
    for tg, name, gender in specs:
        await _join_via_link(bot, code, tg, name, gender)

    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
    await _poll_group(bot, group, LESSON)
    async with session_scope() as s:
        survey = await surveys_svc.find_attendance_survey(s, group_id, LESSON)
        survey_id = survey.id
    for tg, name, _ in specs:
        msg = bot.last_to(tg)
        await on_attendance(
            FakeCallback(
                f"att:{survey_id}:yes", fake_user(tg, full_name=name), message=msg
            )
        )

    # add a pool member so picker keyboard appears
    async with session_scope() as s:
        sibling = await groups_svc.create_group(
            s, admin_id, "Другая", "2", "20:00", 18
        )
        sup, _ = await persons_svc.add_to_group(s, sibling, 910, "u", "Саппорт")
        await persons_svc.set_gender(s, sup.id, Gender.male)
        await persons_svc.set_support_level(s, sup.id, 2)
        sup_id = sup.id

    await _maybe_recruit(bot, group, LESSON, LESSON - timedelta(hours=3))
    async with session_scope() as s:
        call = await supports_svc.get_call_for_survey(s, survey_id)
        call_id = call.id
        assert call.status == SupportCallStatus.pending

    picker = bot.last_to(TEACHER_TG)
    assert "не хватает" in picker.text

    # outsider teacher
    async with session_scope() as s:
        await admins_svc.get_or_create(s, 999, "x", "Чужой")
    foreign = fake_user(999, full_name="Чужой")

    # 9a. foreign tries to toggle
    await cb_support_pick(
        FakeCallback(f"pick:{call_id}:{sup_id}", foreign, message=picker)
    )
    # 9b. foreign tries to send
    await cb_support_pick_send(
        FakeCallback(f"picksend:{call_id}", foreign, message=picker), bot
    )

    async with session_scope() as s:
        call = await supports_svc.get_call(s, call_id)
        assert call.status == SupportCallStatus.pending  # untouched
    # no invitations went to the pool member
    assert not bot.sent_to(910)


# ── 10. support oversubscription ─────────────────────────────────────


async def test_support_oversubscription(db, monkeypatch) -> None:
    bot = FakeBot()
    group_id = await _create_group_via_wizard(bot, monkeypatch)
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        admin_id = group.admin_id
        code = group.join_code

    # 1 male, 3 female → deficit 2
    specs = [
        (1001, "M1", Gender.male),
        (1002, "F1", Gender.female),
        (1003, "F2", Gender.female),
        (1004, "F3", Gender.female),
    ]
    for tg, name, gender in specs:
        await _join_via_link(bot, code, tg, name, gender)

    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
    await _poll_group(bot, group, LESSON)
    async with session_scope() as s:
        survey = await surveys_svc.find_attendance_survey(s, group_id, LESSON)
        survey_id = survey.id
    for tg, name, _ in specs:
        msg = bot.last_to(tg)
        await on_attendance(
            FakeCallback(
                f"att:{survey_id}:yes", fake_user(tg, full_name=name), message=msg
            )
        )

    # three pool members
    async with session_scope() as s:
        sibling = await groups_svc.create_group(
            s, admin_id, "Другая", "2", "20:00", 18
        )
        pool_ids = []
        for tg, name in ((1101, "S1"), (1102, "S2"), (1103, "S3")):
            sup, _ = await persons_svc.add_to_group(s, sibling, tg, "u", name)
            await persons_svc.set_gender(s, sup.id, Gender.male)
            await persons_svc.set_support_level(s, sup.id, 2)
            pool_ids.append((sup.id, tg))

    await _maybe_recruit(bot, group, LESSON, LESSON - timedelta(hours=3))
    async with session_scope() as s:
        call = await supports_svc.get_call_for_survey(s, survey_id)
        call_id = call.id
        assert call.needed == 2

    picker = bot.last_to(TEACHER_TG)
    teacher = fake_user(TEACHER_TG, full_name="Препод")
    # tick all three, then send
    for pid, _ in pool_ids:
        await cb_support_pick(
            FakeCallback(f"pick:{call_id}:{pid}", teacher, message=picker)
        )
    await cb_support_pick_send(
        FakeCallback(f"picksend:{call_id}", teacher, message=picker), bot
    )

    # first two join → fill 2/2, call closed
    sup1_pid, sup1_tg = pool_ids[0]
    sup2_pid, sup2_tg = pool_ids[1]
    sup3_pid, sup3_tg = pool_ids[2]

    for tg in (sup1_tg, sup2_tg):
        invite = bot.last_to(tg)
        await cb_support_join(
            FakeCallback(
                f"sup:join:{call_id}", fake_user(tg, full_name="X"), message=invite
            ),
            bot,
        )

    async with session_scope() as s:
        c = await supports_svc.get_call(s, call_id)
        assert c.status == SupportCallStatus.closed
        helped_3 = (await s.get(Person, sup3_pid)).helped_count

    # third tries — too late
    invite3 = bot.last_to(sup3_tg)
    await cb_support_join(
        FakeCallback(
            f"sup:join:{call_id}", fake_user(sup3_tg, full_name="X"), message=invite3
        ),
        bot,
    )
    assert "Уже набрали" in invite3.text

    async with session_scope() as s:
        assert (await s.get(Person, sup3_pid)).helped_count == helped_3  # unchanged
        assert (await s.get(Person, sup1_pid)).helped_count == 1
        assert (await s.get(Person, sup2_pid)).helped_count == 1


# ── 11. pool level cycle ─────────────────────────────────────────────


async def test_pool_level_cycle(db, monkeypatch) -> None:
    bot = FakeBot()
    group_id = await _create_group_via_wizard(bot, monkeypatch)
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        person, _ = await persons_svc.add_to_group(s, group, 1200, "u", "Кандидат")
        await persons_svc.set_gender(s, person.id, Gender.male)
        person_id = person.id

    teacher = fake_user(TEACHER_TG, full_name="Препод")

    expected = [1, 2, 3, None]
    for want in expected:
        await cb_pool_toggle(
            FakeCallback(
                f"tpool:{person_id}", teacher, message=SentMessage(None, TEACHER_TG, "")
            )
        )
        async with session_scope() as s:
            assert (await s.get(Person, person_id)).support_level == want


# ── 12. stats shows recent closed surveys ────────────────────────────


async def test_stats_shows_recent(db, monkeypatch) -> None:
    bot = FakeBot()
    group_id = await _create_group_via_wizard(bot, monkeypatch)
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        code = group.join_code

    specs = [
        (1301, "S1", Gender.female),
        (1302, "S2", Gender.female),
        (1303, "S3", Gender.male),
        (1304, "S4", Gender.male),
    ]
    for tg, name, gender in specs:
        await _join_via_link(bot, code, tg, name, gender)

    # three past lessons — record different yes counts
    base = datetime(2026, 5, 11, 19, 0)
    lessons = [base, base + timedelta(days=7), base + timedelta(days=14)]
    yes_counts = [4, 3, 2]  # of 4 each → total 9/12 → 75%

    async with session_scope() as s:
        members = await persons_svc.group_members(s, group_id)
        for lesson_at, yes_n in zip(lessons, yes_counts, strict=False):
            survey, _ = await surveys_svc.get_or_create_survey(
                s, group_id, SurveyKind.attendance, lesson_at
            )
            await surveys_svc.ensure_responses(s, survey.id, [p.id for p in members])
            for p, idx in zip(members, range(len(members)), strict=False):
                ans = Answer.yes if idx < yes_n else Answer.no
                await surveys_svc.record_answer(s, survey.id, p.id, ans)
        # close them all
        await surveys_svc.close_past_surveys(s, datetime(2026, 5, 30, 0, 0))

    teacher = fake_user(TEACHER_TG, full_name="Препод")
    today_msg = FakeMessage(teacher, bot=bot)
    await cmd_stats(today_msg)
    assert today_msg.replies
    text = today_msg.replies[0].text
    assert "Бачата" in text  # group name from wizard default
    # total: 9/12 → 75
    assert "средний отклик: <b>75%</b>" in text


# ── 13. /grant — developer-only handler ──────────────────────────────


async def test_grant_makes_teacher(db, monkeypatch) -> None:
    bot = FakeBot()
    monkeypatch.setattr(settings, "developer_id", 7777)

    # 13a — developer grants someone
    dev = fake_user(7777, full_name="Dev")
    dev_msg = FakeMessage(dev, bot=bot)
    cmd_obj = SimpleNamespace(args="999")
    await cmd_grant(dev_msg, cmd_obj, bot)
    async with session_scope() as s:
        t = await admins_svc.get_by_tg_id(s, 999)
        assert t is not None
    # one reply to dev_msg.answer (dev), one bot.send_message to 999
    assert dev_msg.replies and "теперь преподаватель" in dev_msg.replies[0].text
    notify = bot.sent_to(999)
    assert notify and "выдан статус" in notify[0].text

    # 13b — non-developer cannot grant
    bot2 = FakeBot()
    other = fake_user(123, full_name="Хитрец")
    other_msg = FakeMessage(other, bot=bot2)
    await cmd_grant(other_msg, SimpleNamespace(args="888"), bot2)
    async with session_scope() as s:
        assert await admins_svc.get_by_tg_id(s, 888) is None
    assert other_msg.replies == []  # silent
    assert bot2.sent == []


# ── 14. full scheduler tick ──────────────────────────────────────────


async def test_full_tick(db, monkeypatch) -> None:
    bot = FakeBot()
    group_id = await _create_group_via_wizard(bot, monkeypatch)
    async with session_scope() as s:
        group = await groups_svc.get_group(s, group_id)
        admin_id = group.admin_id
        code = group.join_code

    # 1 male + 4 female yes → imbalance 3, threshold 2
    specs = [
        (1401, "M1", Gender.male),
        (1402, "F1", Gender.female),
        (1403, "F2", Gender.female),
        (1404, "F3", Gender.female),
        (1405, "F4", Gender.female),
    ]
    for tg, name, gender in specs:
        await _join_via_link(bot, code, tg, name, gender)

    # Make a pool candidate so a picker keyboard is sent
    async with session_scope() as s:
        sibling = await groups_svc.create_group(
            s, admin_id, "Другая", "2", "20:00", 18
        )
        sup, _ = await persons_svc.add_to_group(s, sibling, 1410, "u", "Помощник")
        await persons_svc.set_gender(s, sup.id, Gender.male)
        await persons_svc.set_support_level(s, sup.id, 2)

    # Pin the scheduler clock: 2h before lesson (after cutoff=6h, well past half-window)
    pinned_now = LESSON - timedelta(hours=2)
    monkeypatch.setattr(sched_module, "local_now", lambda: pinned_now)

    # Each student should answer "yes" BEFORE the tick — but the tick is what
    # creates the survey. So: tick #1 sends polls, then students answer, then tick #2
    # measures imbalance and recruits + summary.
    # To do it all in one tick: pre-seed the survey + yes responses manually so
    # the scheduler's tick observes them in a single pass.
    async with session_scope() as s:
        survey, _ = await surveys_svc.get_or_create_survey(
            s, group_id, SurveyKind.attendance, LESSON
        )
        members = await persons_svc.group_members(s, group_id)
        await surveys_svc.ensure_responses(s, survey.id, [p.id for p in members])
        for p in members:
            await surveys_svc.record_answer(s, survey.id, p.id, Answer.yes)

    teacher_before = len(bot.sent_to(TEACHER_TG))
    await sched_module.tick(bot)

    # poll wasn't re-created (we pre-made the survey), so no new student polls;
    # but the auto-summary AND recruit picker both went to the teacher
    teacher_msgs = bot.sent_to(TEACHER_TG)[teacher_before:]
    summary_msgs = [m for m in teacher_msgs if "итоги опроса" in m.text]
    picker_msgs = [m for m in teacher_msgs if "не хватает" in m.text]
    assert len(summary_msgs) == 1
    assert len(picker_msgs) == 1

    async with session_scope() as s:
        survey = await surveys_svc.find_attendance_survey(s, group_id, LESSON)
        assert survey is not None and survey.summary_sent is True
        call = await supports_svc.get_call_for_survey(s, survey.id)
        assert call is not None
        assert call.status == SupportCallStatus.pending
        assert call.needed == 3

    # picker reply_markup should reference the pool member
    picker = picker_msgs[0]
    pick_btns = [
        b.callback_data
        for row in picker.reply_markup.inline_keyboard
        for b in row
        if (b.callback_data or "").startswith("pick:")
    ]
    assert any(":1410" not in c for c in pick_btns)  # pick uses person_id, not tg_id
    assert pick_btns  # at least one candidate


