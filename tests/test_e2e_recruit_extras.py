"""E2E for the recruit edge flows not hit by the happy-path scenario: a support
changing their mind (skip → unclaim), expanding the picker to colleagues, the
late-join guard, and the already-claimed flip."""
from __future__ import annotations

from datetime import timedelta

from app.bot.handlers.recruit import (
    cb_support_join,
    cb_support_pick,
    cb_support_pick_send,
    cb_support_pickmore,
    cb_support_skip,
)
from app.bot.handlers.survey import on_attendance
from app.db.models import Gender, SupportCallStatus
from app.db.session import session_scope
from app.scheduler.scheduler import _maybe_recruit, _poll_group
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import supports as supports_svc
from app.services import surveys as surveys_svc
from tests._e2e_helpers import LESSON, TEACHER_TG, cb, join_via_link, make_group
from tests._fakes import FakeBot
from tests._outbox_helpers import drain_outbox


async def _deficit_call(bot: FakeBot, monkeypatch, *, needed_pool: bool = True):
    """Build a group with a male deficit, poll, everyone says yes, run _maybe_recruit.
    Returns (gid, sid, survey_id, call_id)."""
    monkeypatch.setattr(
        "app.bot.handlers.recruit.utc_now", lambda: LESSON - timedelta(hours=2)
    )
    gid, sid, code = await make_group()
    specs = [
        (1001, "M1", Gender.male),
        (1002, "F1", Gender.female),
        (1003, "F2", Gender.female),
        (1004, "F3", Gender.female),
    ]
    for tg, name, g in specs:
        await join_via_link(bot, code, tg, name, g)

    async with session_scope() as s:
        group = await groups_svc.get_group(s, gid)
    await _poll_group(bot, group, LESSON, LESSON - timedelta(hours=10))
    await drain_outbox(bot)
    async with session_scope() as s:
        survey = await surveys_svc.find_attendance_survey(s, gid, LESSON)
        survey_id = survey.id
    for tg, name, _ in specs:
        await on_attendance(
            cb(f"att:{survey_id}:yes", tg, name=name, message=bot.last_to(tg))
        )

    if needed_pool:
        async with session_scope() as s:
            sibling = await groups_svc.create_group(s, sid, "Друг", "2", "20:00", 18)
            sup, _ = await persons_svc.add_to_group(s, sibling, 1101, "u", "Витя")
            await persons_svc.set_gender(s, sup.id, Gender.male)
            await persons_svc.set_pool_level(s, sid, sup.id, 2)

    async with session_scope() as s:
        group = await groups_svc.get_group(s, gid)
    await _maybe_recruit(bot, group, LESSON, LESSON - timedelta(hours=2))
    await drain_outbox(bot)
    async with session_scope() as s:
        call = await supports_svc.get_call_for_survey(s, survey_id)
        return gid, sid, survey_id, call.id


async def test_support_changes_mind(db, monkeypatch) -> None:
    bot = FakeBot()
    gid, sid, survey_id, call_id = await _deficit_call(bot, monkeypatch)

    # teacher picks the pool member and sends the invite
    picker = bot.last_to(TEACHER_TG)
    async with session_scope() as s:
        sup = await persons_svc.get_person(s, sid, 1101)
        sup_pid = sup.id
        # remember the picker message so the balance bar refresh path runs
        await supports_svc.set_picker_ref(s, call_id, TEACHER_TG, 4242)
    await cb_support_pick(cb(f"pick:{call_id}:{sup_pid}", TEACHER_TG, message=picker))
    await cb_support_pick_send(cb(f"picksend:{call_id}", TEACHER_TG, message=picker), bot)
    await drain_outbox(bot)

    # the support says yes…
    invite = bot.last_to(1101)
    await cb_support_join(
        cb(f"sup:join:{call_id}", 1101, name="Витя", message=invite), bot
    )
    async with session_scope() as s:
        assert await supports_svc.count_claims(s, call_id) == 1

    # …then changes their mind via "Не смогу"
    sent_before = len(bot.sent_to(TEACHER_TG))
    await cb_support_skip(
        cb(f"sup:skip:{call_id}", 1101, name="Витя", message=bot.last_to(1101)), bot
    )
    async with session_scope() as s:
        assert await supports_svc.count_claims(s, call_id) == 0  # unclaimed
    # teacher got the "передумал" ping
    new = bot.sent_to(TEACHER_TG)[sent_before:]
    assert any("передумал" in m.text for m in new)


async def test_pickmore_expand(db, monkeypatch) -> None:
    bot = FakeBot()
    gid, sid, survey_id, call_id = await _deficit_call(bot, monkeypatch)
    picker = bot.last_to(TEACHER_TG)
    # tapping "из пула коллег" re-renders the picker without error
    more = cb(f"pickmore:{call_id}", TEACHER_TG, message=picker)
    await cb_support_pickmore(more)
    assert picker.reply_markup is not None  # re-rendered


async def test_late_join_guard(db, monkeypatch) -> None:
    bot = FakeBot()
    gid, sid, survey_id, call_id = await _deficit_call(bot, monkeypatch)
    picker = bot.last_to(TEACHER_TG)
    async with session_scope() as s:
        sup = await persons_svc.get_person(s, sid, 1101)
        sup_pid = sup.id
    await cb_support_pick(cb(f"pick:{call_id}:{sup_pid}", TEACHER_TG, message=picker))
    await cb_support_pick_send(cb(f"picksend:{call_id}", TEACHER_TG, message=picker), bot)
    await drain_outbox(bot)

    # now the lesson is in the past → late join is refused
    monkeypatch.setattr(
        "app.bot.handlers.recruit.utc_now", lambda: LESSON + timedelta(hours=1)
    )
    invite = bot.last_to(1101)
    await cb_support_join(
        cb(f"sup:join:{call_id}", 1101, name="Витя", message=invite), bot
    )
    assert "добор закрыт" in invite.text
    async with session_scope() as s:
        assert await supports_svc.count_claims(s, call_id) == 0


async def test_already_claimed_flip(db, monkeypatch) -> None:
    bot = FakeBot()
    gid, sid, survey_id, call_id = await _deficit_call(bot, monkeypatch)
    # bump needed to 2 so a single claim doesn't close the call
    async with session_scope() as s:
        call = await supports_svc.get_call(s, call_id)
        call.needed = 2
        call.status = SupportCallStatus.open
        await s.flush()
        sup = await persons_svc.get_person(s, sid, 1101)
        await supports_svc.claim(s, call_id, sup.id)

    invite = bot.last_to(TEACHER_TG)  # any message object to edit
    # second join by the same person → "уже записан" + flip keyboard
    await cb_support_join(
        cb(f"sup:join:{call_id}", 1101, name="Витя", message=invite), bot
    )
    assert "записан саппортом" in invite.text
