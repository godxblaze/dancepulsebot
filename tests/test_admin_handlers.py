"""Tests for teacher commands and the new-group wizard."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.bot.handlers.admin import (
    _owned_group,
    cb_group_card,
    cb_group_delete,
    cb_group_invite,
    cb_group_students,
    cb_groups_list,
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
from app.bot.states import NewGroup
from app.db.models import Answer, SurveyKind
from app.db.session import session_scope
from app.services import groups, persons, surveys, admins

# ── fakes ────────────────────────────────────────────────────────────


class FakeMessage:
    def __init__(self, user_id: int, text: str | None = None) -> None:
        self.from_user = SimpleNamespace(
            id=user_id, full_name="Препод Тест", username="teacher"
        )
        self.text = text
        self.replies: list[str] = []
        self.photo_captions: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.replies.append(text)

    async def answer_photo(self, photo, caption: str | None = None, **kwargs) -> None:
        self.photo_captions.append(caption or "")


class FakeCbMessage:
    def __init__(self) -> None:
        self.text: str | None = None
        self.reply_markup = None
        self.photo_captions: list[str] = []

    async def edit_text(self, text: str, **kwargs) -> None:
        self.text = text
        self.reply_markup = kwargs.get("reply_markup")

    async def edit_reply_markup(self, **kwargs) -> None:
        self.reply_markup = kwargs.get("reply_markup")

    async def answer(self, text: str, **kwargs) -> None:
        self.text = text

    async def answer_photo(self, photo, caption: str | None = None, **kwargs) -> None:
        self.photo_captions.append(caption or "")


class FakeCallback:
    def __init__(self, data: str, user_id: int) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeCbMessage()
        self.answers: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)


class FakeFSM:
    """Minimal stand-in for aiogram's FSMContext."""

    def __init__(self) -> None:
        self.state = None
        self.data: dict = {}

    async def set_state(self, state) -> None:
        self.state = state

    async def get_data(self) -> dict:
        return dict(self.data)

    async def update_data(self, **kwargs) -> dict:
        self.data.update(kwargs)
        return dict(self.data)

    async def clear(self) -> None:
        self.state = None
        self.data = {}


@pytest.fixture
def freeze_today(monkeypatch):
    """Pin the clock used by /today."""

    def _set(dt: datetime) -> None:
        monkeypatch.setattr("app.bot.handlers.admin.local_now", lambda: dt)

    return _set


# ── /today ───────────────────────────────────────────────────────────


async def test_today_no_teacher(db) -> None:
    """Non-teacher who is also not a registered student falls through to the
    student today screen, which nudges them to ask their teacher for a link."""
    msg = FakeMessage(user_id=1)
    await cmd_today(msg)
    assert msg.replies and "попроси у преподавателя" in msg.replies[0]


async def test_today_teacher_no_lessons(db) -> None:
    async with session_scope() as s:
        await admins.get_or_create(s, 1, "t", "T")
    msg = FakeMessage(user_id=1)
    await cmd_today(msg)
    assert msg.replies == ["На сегодня и завтра занятий нет."]


async def test_today_lesson_tomorrow_no_survey(db, freeze_today) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 10, "t", "T")
        await groups.create_group(s, t.id, "Сальса", "0", "19:00", 18)
    freeze_today(datetime(2026, 5, 24, 12, 0))  # Sunday -> lesson Monday (tomorrow)
    msg = FakeMessage(user_id=10)
    await cmd_today(msg)
    assert len(msg.replies) == 1
    assert "завтра" in msg.replies[0]
    assert "опрос ещё не разослан" in msg.replies[0]


async def test_today_lesson_today_with_survey(db, freeze_today) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 10, "t", "T")
        g = await groups.create_group(s, t.id, "Хип-хоп", "0", "19:00", 18)
        p, _ = await persons.add_to_group(s, g, 50, "u", "Ученик")
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, datetime(2026, 5, 25, 19, 0)
        )
        await surveys.ensure_responses(s, survey.id, [p.id])
        await surveys.record_answer(s, survey.id, p.id, Answer.yes)
    freeze_today(datetime(2026, 5, 25, 9, 0))  # Monday, lesson today
    msg = FakeMessage(user_id=10)
    await cmd_today(msg)
    assert "сегодня" in msg.replies[0]
    assert "придут <b>1/1</b>" in msg.replies[0]


# ── /stats ───────────────────────────────────────────────────────────


async def test_stats_no_groups(db) -> None:
    async with session_scope() as s:
        await admins.get_or_create(s, 1, "t", "T")
    msg = FakeMessage(user_id=1)
    await cmd_stats(msg)
    assert "нет групп" in msg.replies[0]


async def test_stats_non_teacher_silent(db) -> None:
    msg = FakeMessage(user_id=999)
    await cmd_stats(msg)
    assert msg.replies == []


async def test_stats_no_closed_surveys(db) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 10, "t", "T")
        await groups.create_group(s, t.id, "G", "0", "19:00", 18)
    msg = FakeMessage(user_id=10)
    await cmd_stats(msg)
    assert "пока нет завершённых опросов" in msg.replies[0]


async def test_stats_with_closed_surveys(db) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 10, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        p, _ = await persons.add_to_group(s, g, 50, "u", "Ученик")
        for day in (11, 18):
            survey, _ = await surveys.get_or_create_survey(
                s, g.id, SurveyKind.attendance, datetime(2026, 5, day, 19, 0)
            )
            await surveys.ensure_responses(s, survey.id, [p.id])
            await surveys.record_answer(s, survey.id, p.id, Answer.yes)
        await surveys.close_past_surveys(s, datetime(2026, 5, 20, 0, 0))
    msg = FakeMessage(user_id=10)
    await cmd_stats(msg)
    text = msg.replies[0]
    assert "последние 2 занятий" in text
    assert "средний отклик: <b>100%</b>" in text


# ── _owned_group authorization ───────────────────────────────────────


async def test_owned_group_owner(db) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 10, "t", "Owner")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        gid = g.id
    async with session_scope() as s:
        owned = await _owned_group(s, gid, 10)
        assert owned is not None and owned.id == gid


async def test_owned_group_rejects_other_teacher(db) -> None:
    async with session_scope() as s:
        owner = await admins.get_or_create(s, 10, "o", "Owner")
        await admins.get_or_create(s, 20, "x", "Stranger")
        g = await groups.create_group(s, owner.id, "G", "0", "19:00", 18)
        gid = g.id
    async with session_scope() as s:
        assert await _owned_group(s, gid, 20) is None  # not the owner


async def test_owned_group_unknown_group(db) -> None:
    async with session_scope() as s:
        await admins.get_or_create(s, 10, "t", "T")
    async with session_scope() as s:
        assert await _owned_group(s, 999, 10) is None


# ── new-group wizard ─────────────────────────────────────────────────


async def test_wizard_rejects_empty_style(db) -> None:
    async with session_scope() as s:
        await admins.get_or_create(s, 10, "t", "T")
    fsm = FakeFSM()
    await newgroup_start(FakeMessage(user_id=10), fsm)
    assert fsm.state == NewGroup.style
    # switch to free-typing then send blank text — wizard must hold the state
    await newgroup_style_pick(FakeCallback("ngst:type", 10), fsm)
    assert fsm.state == NewGroup.style_typing
    msg = FakeMessage(user_id=10, text="   ")
    await newgroup_style_typed(msg, fsm)
    assert fsm.state == NewGroup.style_typing  # not advanced
    assert any("Пустое направление" in r for r in msg.replies)


async def test_wizard_rejects_bad_time(db) -> None:
    fsm = FakeFSM()
    fsm.data = {"name": "G", "weekdays": [0]}
    fsm.state = NewGroup.lesson_time
    msg = FakeMessage(user_id=10, text="25:99")
    await newgroup_time(msg, fsm, None)
    assert any("Не похоже на время" in r for r in msg.replies)
    async with session_scope() as s:
        assert await admins.get_by_tg_id(s, 10) is None  # nothing created


async def test_wizard_full_flow_creates_group(db, monkeypatch) -> None:
    async def fake_username(bot) -> str:
        return "dancepulsebot"

    monkeypatch.setattr("app.bot.handlers.admin.get_bot_username", fake_username)

    async with session_scope() as s:
        await admins.get_or_create(s, 30, "teacher", "Препод Тест")

    fsm = FakeFSM()
    await newgroup_start(FakeMessage(user_id=30), fsm)
    assert fsm.state == NewGroup.style

    await newgroup_style_pick(FakeCallback("ngst:type", 30), fsm)
    assert fsm.state == NewGroup.style_typing
    await newgroup_style_typed(FakeMessage(user_id=30, text="Сальса"), fsm)
    assert fsm.state == NewGroup.level

    await newgroup_level_pick(FakeCallback("nglv:pick:2", 30), fsm)
    assert fsm.state == NewGroup.weekdays

    await newgroup_weekdays(FakeCallback("wd:0", 30), fsm)  # Monday on
    await newgroup_weekdays(FakeCallback("wd:2", 30), fsm)  # Wednesday on
    await newgroup_weekdays(FakeCallback("wd:done", 30), fsm)
    assert fsm.state == NewGroup.lesson_time

    msg = FakeMessage(user_id=30, text="19:00")
    await newgroup_time(msg, fsm, None)
    assert fsm.state == NewGroup.venue
    await newgroup_venue_pick(FakeCallback("ngvn:skip", 30), fsm)
    assert fsm.state == NewGroup.room
    await newgroup_room_skip(FakeCallback("ngrm:skip", 30), fsm)
    assert fsm.state == NewGroup.teachers_note
    final = FakeCallback("ngtn:skip", 30)
    await newgroup_teachers_skip(final, fsm, None)

    async with session_scope() as s:
        t = await admins.get_by_tg_id(s, 30)
        assert t is not None
        gs = await groups.admin_groups(s, t.id)
    assert len(gs) == 1
    assert gs[0].style == "Сальса"
    assert gs[0].level == 2
    assert gs[0].name == "Сальса · ур.2"
    assert gs[0].weekdays == "0,2"
    assert gs[0].lesson_time == "19:00"


async def test_wizard_rejects_non_teacher(db) -> None:
    fsm = FakeFSM()
    msg = FakeMessage(user_id=999)
    await newgroup_start(msg, fsm)
    assert fsm.state is None
    assert msg.replies == []
    async with session_scope() as s:
        assert await admins.get_by_tg_id(s, 999) is None


async def test_wizard_done_with_no_weekdays_blocked(db) -> None:
    fsm = FakeFSM()
    fsm.data = {"name": "G", "weekdays": []}
    fsm.state = NewGroup.weekdays
    call = FakeCallback("wd:done", 10)
    await newgroup_weekdays(call, fsm)
    assert fsm.state == NewGroup.weekdays  # not advanced
    assert any("хотя бы один день" in a for a in call.answers)


# ── group-card callbacks ─────────────────────────────────────────────


async def _make_group_with_members(owner_tg: int = 10, n: int = 2) -> int:
    async with session_scope() as s:
        t = await admins.get_or_create(s, owner_tg, "t", "Owner")
        g = await groups.create_group(
            s, t.id, "Сальса", "0,2", "19:00", 18, style="Сальса"
        )
        for i in range(n):
            await persons.add_to_group(s, g, 600 + i, f"u{i}", f"Ученик {i}")
        return g.id


async def test_cb_group_card_shows_student_count(db) -> None:
    gid = await _make_group_with_members(n=2)
    call = FakeCallback(f"grp:{gid}", 10)
    await cb_group_card(call)
    assert call.message.text is not None
    assert "Учеников: 2" in call.message.text


async def test_cb_group_card_rejects_non_owner(db) -> None:
    gid = await _make_group_with_members(owner_tg=10)
    call = FakeCallback(f"grp:{gid}", 999)  # not the owner
    await cb_group_card(call)
    assert call.message.text is None  # card not rendered
    assert any("не найдена" in a for a in call.answers)


async def test_cb_group_students_lists_names(db) -> None:
    gid = await _make_group_with_members(n=2)
    call = FakeCallback(f"grpstu:{gid}", 10)
    await cb_group_students(call)
    # Names now live in the inline-keyboard buttons, not the message body.
    buttons = [
        btn.text
        for row in call.message.reply_markup.inline_keyboard
        for btn in row
    ]
    assert any("Ученик 0" in t for t in buttons)
    assert any("Ученик 1" in t for t in buttons)


async def test_cb_group_students_empty(db) -> None:
    gid = await _make_group_with_members(n=0)
    call = FakeCallback(f"grpstu:{gid}", 10)
    await cb_group_students(call)
    assert "пока нет учеников" in call.message.text


async def test_cb_group_invite_sends_qr_with_link(db, monkeypatch) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 10, "t", "Owner")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        gid, code = g.id, g.join_code

    async def fake_username(bot) -> str:
        return "dancepulsebot"

    monkeypatch.setattr("app.bot.handlers.admin.get_bot_username", fake_username)
    call = FakeCallback(f"grpinv:{gid}", 10)
    await cb_group_invite(call, None)
    assert call.message.photo_captions, "invite must be sent as a photo"
    caption = call.message.photo_captions[0]
    assert f"start=join_{code}" in caption
    assert "dancepulsebot" in caption


async def test_cb_group_delete_removes_group_and_students(db) -> None:
    gid = await _make_group_with_members(n=2)
    call = FakeCallback(f"grpdelok:{gid}", 10)
    await cb_group_delete(call)
    assert "удалена" in call.message.text
    async with session_scope() as s:
        assert await groups.get_group(s, gid) is None
        assert await persons.count_members(s, gid) == 0  # FK cascade


async def test_cb_group_delete_rejects_non_owner(db) -> None:
    gid = await _make_group_with_members(owner_tg=10)
    call = FakeCallback(f"grpdelok:{gid}", 999)
    await cb_group_delete(call)
    assert any("не найдена" in a for a in call.answers)
    async with session_scope() as s:
        assert await groups.get_group(s, gid) is not None  # not deleted


async def test_cb_groups_list_empty(db) -> None:
    call = FakeCallback("grplist", 555)
    await cb_groups_list(call)
    assert call.message.text is not None and "нет групп" in call.message.text


# ── edit existing group ──────────────────────────────────────────────


async def test_edit_level_updates_group_and_name(db) -> None:
    from app.bot.handlers.admin import cb_edit_level, edit_level_pick

    gid = await _make_group_with_members(n=0)
    fsm = FakeFSM()
    # open the level picker
    await cb_edit_level(FakeCallback(f"ge:level:{gid}", 10), fsm)
    assert fsm.state.state.endswith(":level")  # EditGroup.level
    # pick «ур.3»
    await edit_level_pick(FakeCallback("nglv:pick:3", 10), fsm)
    assert fsm.state is None
    async with session_scope() as s:
        g = await groups.get_group(s, gid)
        assert g.level == 3
        # Сальса was set in _make_group_with_members → name derives from style+level
        assert g.name == "Сальса · ур.3"


async def test_edit_room_typed_clears_when_skip(db) -> None:
    from app.bot.handlers.admin import (
        cb_edit_room,
        edit_room_skip,
        edit_room_typed,
    )

    gid = await _make_group_with_members(n=0)
    fsm = FakeFSM()
    await cb_edit_room(FakeCallback(f"ge:room:{gid}", 10), fsm)
    await edit_room_typed(FakeMessage(user_id=10, text="зал 38"), fsm)
    async with session_scope() as s:
        assert (await groups.get_group(s, gid)).room == "зал 38"
    # second pass: skip clears it
    fsm2 = FakeFSM()
    await cb_edit_room(FakeCallback(f"ge:room:{gid}", 10), fsm2)
    await edit_room_skip(FakeCallback("ngrm:skip", 10), fsm2)
    async with session_scope() as s:
        assert (await groups.get_group(s, gid)).room is None


async def test_edit_rejects_non_owner(db) -> None:
    from app.bot.handlers.admin import cb_edit_level

    gid = await _make_group_with_members(owner_tg=10, n=0)
    fsm = FakeFSM()
    await cb_edit_level(FakeCallback(f"ge:level:{gid}", 999), fsm)  # not the owner
    assert fsm.state is None  # didn't enter the edit flow
    assert any("не найдена" in a for a in FakeCallback(f"ge:level:{gid}", 999).answers) is False
