"""E2E for the student-facing screens (home/today) and the support-onboarding
deep-link flow in student.py."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from app.bot.handlers import student as student_mod
from app.bot.handlers.student import (
    join_gender,
    join_name_typed,
    join_start,
    show_student_home,
    show_student_today,
)
from app.bot.states import JoinFlow
from app.core.config import settings
from app.db.models import Gender
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import venues as venues_svc
from tests._e2e_helpers import LESSON, TEACHER_TG, cb, join_via_link, make_group
from tests._fakes import FakeBot, FakeFSM, FakeMessage, SentMessage, fake_user


async def test_student_home_unknown_user(db, monkeypatch) -> None:
    bot = FakeBot()
    monkeypatch.setattr(settings, "developer_contact", "@dev_contact")
    # a tg with no Person row at all → onboarding screen with the contact line
    msg = FakeMessage(fake_user(9001, full_name="Незнакомец"), bot=bot)
    await show_student_home(msg)
    assert msg.replies
    text = msg.replies[0].text
    assert "Dance Pulse" in text and "@dev_contact" in text

    # no developer_contact → the fallback teacher line shows the tg id
    monkeypatch.setattr(settings, "developer_contact", "")
    msg2 = FakeMessage(fake_user(9002), bot=bot)
    await show_student_home(msg2)
    assert "9002" in msg2.replies[0].text


async def test_student_home_and_today_with_group(db, monkeypatch) -> None:
    bot = FakeBot()
    gid, sid, code = await make_group()
    # attach a venue so today's screen exercises format_place
    async with session_scope() as s:
        venue = await venues_svc.create_venue(s, sid, "Студия Полевик", "Шлюзовая 8")
        group = await groups_svc.get_group(s, gid)
        group.venue_id = venue.id
        group.room = "зал 3"
        await groups_svc.update_group(s, group)
    await join_via_link(bot, code, 950, "Аня", Gender.female)

    # pin both student-side clocks to lesson day so "today" matches
    now = LESSON - timedelta(hours=4)
    monkeypatch.setattr(student_mod, "utc_now", lambda: now)

    home = FakeMessage(fake_user(950, full_name="Аня"), bot=bot)
    await show_student_home(home)
    assert home.replies
    assert "Твои группы" in home.replies[0].text
    assert "Ближайшее" in home.replies[0].text

    today = FakeMessage(fake_user(950, full_name="Аня"), bot=bot)
    await show_student_today(today)
    assert today.replies
    assert "Полевик" in today.replies[0].text  # venue rendered via format_place


async def test_student_today_no_lessons(db) -> None:
    bot = FakeBot()
    # a) unknown tg — no Person row at all → "не записан"
    unknown = FakeMessage(fake_user(961, full_name="Призрак"), bot=bot)
    await show_student_today(unknown)
    assert unknown.replies and "не записан" in unknown.replies[0].text

    # b) a person in the school but in no group → "занятий нет"
    async with session_scope() as s:
        await admins_svc.get_or_create(s, TEACHER_TG, None, "Препод")
        t = await admins_svc.get_by_tg_id(s, TEACHER_TG)
        from app.services import schools as schools_svc
        school = await schools_svc.get_owned_school(s, t.id)
        await persons_svc.ensure_person(s, school.id, 960, "u", "Одиночка")
    today = FakeMessage(fake_user(960, full_name="Одиночка"), bot=bot)
    await show_student_today(today)
    assert today.replies and "занятий нет" in today.replies[0].text


async def test_support_invite_onboarding(db, monkeypatch) -> None:
    bot = FakeBot()
    _, sid, _ = await make_group()
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, TEACHER_TG)
        support_code = await admins_svc.get_or_create_support_code(s, teacher.id)

    # new support opens support_<code>: name → gender → lands straight in the pool
    user = fake_user(970, full_name="Витя")
    fsm = FakeFSM()
    start = FakeMessage(user, bot=bot)
    await join_start(start, SimpleNamespace(args=f"support_{support_code}"), fsm, bot)
    assert fsm.state == JoinFlow.name
    assert any("саппорт" in r.text.lower() for r in start.replies)

    await join_name_typed(FakeMessage(user, text="Витя", bot=bot), fsm)
    assert fsm.state == JoinFlow.gender
    gender_cb = cb("join:gender:male", 970, name="Витя", message=SentMessage(None, 970, ""))
    await join_gender(gender_cb, fsm, bot)
    assert fsm.state is None

    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, TEACHER_TG)
        levels = await persons_svc.admin_pool_levels(s, teacher.id)
        person = await persons_svc.get_person(s, sid, 970)
        assert levels.get(person.id) == 1  # landed in the pool at level 1
    # teacher got the "готов выручать" ping
    assert any("выручать" in m.text for m in bot.sent_to(TEACHER_TG))


async def test_support_invite_known_person_fastpath(db) -> None:
    bot = FakeBot()
    gid, sid, code = await make_group()
    # someone already known in the school (joined a group, has gender)
    await join_via_link(bot, code, 980, "Слава", Gender.male)
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, TEACHER_TG)
        support_code = await admins_svc.get_or_create_support_code(s, teacher.id)

    user = fake_user(980, full_name="Слава")
    fsm = FakeFSM()
    start = FakeMessage(user, bot=bot)
    await join_start(start, SimpleNamespace(args=f"support_{support_code}"), fsm, bot)
    # known → no FSM, straight into pool
    assert fsm.state is None
    assert any("пул" in r.text.lower() for r in start.replies)
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, TEACHER_TG)
        levels = await persons_svc.admin_pool_levels(s, teacher.id)
        person = await persons_svc.get_person(s, sid, 980)
        assert levels.get(person.id) == 1


async def test_support_invite_bad_code(db) -> None:
    bot = FakeBot()
    await make_group()
    user = fake_user(990, full_name="X")
    start = FakeMessage(user, bot=bot)
    await join_start(start, SimpleNamespace(args="support_garbage"), FakeFSM(), bot)
    assert any("не найдена" in r.text or "устарела" in r.text for r in start.replies)


async def test_join_unrecognized_payload(db) -> None:
    bot = FakeBot()
    user = fake_user(991, full_name="X")
    start = FakeMessage(user, bot=bot)
    await join_start(start, SimpleNamespace(args="weird_payload"), FakeFSM(), bot)
    assert any("не распознана" in r.text for r in start.replies)
