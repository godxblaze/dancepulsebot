"""E2E for the admin command surface: help/claim, /today & /stats (+ export),
the support-pool config, group/person cards, ping, search, responsible-admins."""
from __future__ import annotations

from datetime import timedelta

from app.bot.handlers import _access as access_mod
from app.bot.handlers import admin as admin_mod
from app.bot.handlers.admin import (
    cb_group_admin_toggle,
    cb_group_admins,
    cb_group_card,
    cb_group_invite,
    cb_group_pause,
    cb_group_resume,
    cb_group_students,
    cb_groups_list,
    cb_person_add_to_group,
    cb_person_card,
    cb_person_card_level,
    cb_person_gender_toggle,
    cb_person_remove,
    cb_person_remove_confirm,
    cb_ping_pending,
    cb_stats_export,
    cb_stats_scope,
    cb_students_page,
    cb_today_switch,
    cmd_claim,
    cmd_export,
    cmd_groups,
    cmd_help,
    cmd_myid,
    cmd_stats,
    cmd_today,
)
from app.bot.handlers.pool import (
    cb_cfg_evening,
    cb_cfg_mode,
    cb_cfg_threshold,
    cb_cfg_topup,
    cb_pool_back,
    cb_pool_invite,
    cb_pool_page,
    cb_pool_settings,
    cmd_pool,
)
from app.bot.handlers.search import cb_search_start, on_search_query
from app.bot.states import EntitySearch
from app.core.config import settings
from app.db.models import Answer, Gender, SurveyKind
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import schools as schools_svc
from app.services import surveys as surveys_svc
from app.services.schedule import next_lesson_at
from tests._e2e_helpers import LESSON, TEACHER_TG, cb, join_via_link, make_group
from tests._fakes import FakeBot, FakeFSM, FakeMessage, SentMessage, fake_user

# ── help / myid / claim ──────────────────────────────────────────────


async def test_help_myid_claim(db, monkeypatch) -> None:
    bot = FakeBot()
    await make_group()  # makes TEACHER_TG an admin

    admin_help = FakeMessage(fake_user(TEACHER_TG), bot=bot)
    await cmd_help(admin_help)
    assert "/pool" in admin_help.replies[0].text  # admin help variant

    student_help = FakeMessage(fake_user(123), bot=bot)
    await cmd_help(student_help)
    assert "/pool" not in student_help.replies[0].text  # student help variant

    myid = FakeMessage(fake_user(555), bot=bot)
    await cmd_myid(myid)
    assert "555" in myid.replies[0].text

    # /claim with a secret configured
    monkeypatch.setattr(settings, "owner_claim_secret", "sezam")
    new_admin = FakeMessage(fake_user(600), bot=bot)
    await cmd_claim(new_admin, type("C", (), {"args": "sezam"})(), bot)
    assert "преподаватель" in new_admin.replies[0].text
    async with session_scope() as s:
        assert await admins_svc.get_by_tg_id(s, 600) is not None

    wrong = FakeMessage(fake_user(601), bot=bot)
    await cmd_claim(wrong, type("C", (), {"args": "nope"})(), bot)
    assert "Неверный код" in wrong.replies[0].text

    already = FakeMessage(fake_user(600), bot=bot)
    await cmd_claim(already, type("C", (), {"args": "sezam"})(), bot)
    assert "уже преподаватель" in already.replies[0].text


# ── /today & /stats & export ─────────────────────────────────────────


async def _seed_today_survey(gid: int, now):
    """Create the survey at the group's next lesson with mixed answers + a silent."""
    async with session_scope() as s:
        group = await groups_svc.get_group(s, gid)
        tz = await schools_svc.get_timezone(s, group.school_id)
        lesson_at = next_lesson_at(group.weekdays, group.lesson_time, now, tz=tz)
        survey, _ = await surveys_svc.get_or_create_survey(
            s, gid, SurveyKind.attendance, lesson_at
        )
        members = await persons_svc.group_members(s, gid)
        await surveys_svc.ensure_responses(s, survey.id, [p.id for p in members])
        # first yes, second no, rest silent
        if members:
            await surveys_svc.record_answer(s, survey.id, members[0].id, Answer.yes)
        if len(members) > 1:
            await surveys_svc.record_answer(s, survey.id, members[1].id, Answer.no)
        return survey.id


async def test_today_view(db, monkeypatch) -> None:
    bot = FakeBot()
    gid, sid, code = await make_group()
    for tg, name, g in (
        (501, "Аня", Gender.female),
        (502, "Боря", Gender.male),
        (503, "Вика", Gender.female),
    ):
        await join_via_link(bot, code, tg, name, g)
    now = LESSON - timedelta(hours=4)
    monkeypatch.setattr(admin_mod, "utc_now", lambda: now)
    await _seed_today_survey(gid, now)

    today = FakeMessage(fake_user(TEACHER_TG), bot=bot)
    await cmd_today(today)
    assert today.replies
    text = today.replies[0].text
    assert "придут" in text  # summary line rendered

    # switch to week view, then back to day
    msg = today.replies[0]
    await cb_today_switch(cb("today:week", TEACHER_TG, message=msg))
    await cb_today_switch(cb("today:day", TEACHER_TG, message=msg))
    # invalid span — ignored
    bad = cb("today:bogus", TEACHER_TG, message=msg)
    await cb_today_switch(bad)

    # double-tap same span → Telegram would raise "message is not modified" —
    # handler must swallow it, not crash the callback.
    class _UnmodifiableMessage:
        async def edit_text(self, *args, **kwargs):
            raise Exception("Bad Request: message is not modified")

    dup = cb("today:day", TEACHER_TG, message=_UnmodifiableMessage())
    await cb_today_switch(dup)  # не должно бросить
    assert len(dup.answers) == 1  # call.answer() всё равно вызван


async def test_stats_and_export(db, monkeypatch) -> None:
    bot = FakeBot()
    gid, sid, code = await make_group()
    for tg, name, g in (
        (511, "Аня", Gender.female),
        (512, "Боря", Gender.male),
    ):
        await join_via_link(bot, code, tg, name, g)
    # responsible-admin so the "mine" scope has groups
    async with session_scope() as s:
        t = await admins_svc.get_by_tg_id(s, TEACHER_TG)
        await groups_svc.set_group_admin(s, gid, t.id, True)

    now = LESSON - timedelta(hours=4)
    monkeypatch.setattr(admin_mod, "utc_now", lambda: now)
    monkeypatch.setattr(access_mod, "utc_now", lambda: now)

    # a closed survey with marked actual attendance
    from tests.test_e2e_attendance import _closed_survey_with_roster
    survey_id = await _closed_survey_with_roster(gid)
    async with session_scope() as s:
        members = await persons_svc.group_members(s, gid)
        await surveys_svc.set_actual_attendance(s, survey_id, members[0].id, True)

    stats = FakeMessage(fake_user(TEACHER_TG), bot=bot)
    await cmd_stats(stats)
    assert stats.replies and "средний отклик" in stats.replies[0].text

    msg = stats.replies[0]
    await cb_stats_scope(cb("statsscope:mine", TEACHER_TG, message=msg))
    await cb_stats_scope(cb("statsscope:school", TEACHER_TG, message=msg))

    # double-tap same scope → Telegram "message is not modified" — swallowed.
    class _UnmodifiableMessage:
        async def edit_text(self, *args, **kwargs):
            raise Exception("Bad Request: message is not modified")

    dup = cb("statsscope:school", TEACHER_TG, message=_UnmodifiableMessage())
    await cb_stats_scope(dup)  # не должно бросить
    assert len(dup.answers) == 1

    # export via /export and via the stats button
    exp = FakeMessage(fake_user(TEACHER_TG), bot=bot)
    await cmd_export(exp, bot)
    assert len(exp.replies) == 2  # students.csv + attendance.csv
    assert any(getattr(r, "document", None) is not None for r in exp.replies)

    export_btn = cb("statsexport", TEACHER_TG, message=msg)
    await cb_stats_export(export_btn)
    assert any("CSV" in a[0] for a in export_btn.answers)


# ── support pool config ──────────────────────────────────────────────


async def test_pool_config(db) -> None:
    bot = FakeBot()
    gid, sid, code = await make_group()

    # empty pool first
    empty = FakeMessage(fake_user(TEACHER_TG), bot=bot)
    await cmd_pool(empty, FakeFSM())
    assert "Пул пуст" in empty.replies[0].text

    # add a pool member, re-open
    await join_via_link(bot, code, 521, "Витя", Gender.male)
    async with session_scope() as s:
        t = await admins_svc.get_by_tg_id(s, TEACHER_TG)
        p = await persons_svc.get_person(s, sid, 521)
        await persons_svc.set_pool_level(s, t.id, p.id, 2)

    pool = FakeMessage(fake_user(TEACHER_TG), bot=bot)
    await cmd_pool(pool, FakeFSM())
    assert "пул" in pool.replies[0].text.lower()

    pool_msg = pool.replies[0]
    # open config and tweak every setting
    await cb_pool_settings(cb("poolcfg", TEACHER_TG, message=pool_msg))
    assert "Автодобор" in pool_msg.text
    await cb_cfg_threshold(cb("rcfgt:3", TEACHER_TG, message=pool_msg))
    await cb_cfg_topup(cb("rcfgc:6", TEACHER_TG, message=pool_msg))
    await cb_cfg_evening(cb("rcfge:2000", TEACHER_TG, message=pool_msg))
    await cb_cfg_mode(cb("rcfgmode:open_race", TEACHER_TG, message=pool_msg))
    async with session_scope() as s:
        t = await admins_svc.get_by_tg_id(s, TEACHER_TG)
        assert t.support_threshold == 3
        assert t.support_cutoff_hours == 6
        assert t.recruit_evening_time == "20:00"
        assert t.recruit_mode == "open_race"

    # back to pool, paginate, invite link
    await cb_pool_back(cb("poolback", TEACHER_TG, message=pool_msg), FakeFSM())
    await cb_pool_page(cb("poolpg:0", TEACHER_TG, message=pool_msg), FakeFSM())
    inv = cb("poolinvite", TEACHER_TG, message=pool_msg)
    await cb_pool_invite(inv, bot)
    assert any("Ссылка-приглашение саппорта" in r.text for r in pool_msg.replies)


# ── group card / person card / ping / invite / pause ─────────────────


async def test_group_and_person_cards(db, monkeypatch) -> None:
    bot = FakeBot()
    gid, sid, code = await make_group()
    await join_via_link(bot, code, 531, "Аня", Gender.female)
    now = LESSON - timedelta(hours=4)
    monkeypatch.setattr(access_mod, "utc_now", lambda: now)

    card = SentMessage(bot, TEACHER_TG, "")
    await cb_group_card(cb(f"grp:{gid}", TEACHER_TG, message=card))
    assert "ученик" in card.text.lower()

    # open the students list → person card
    await cb_group_students(cb(f"grpstu:{gid}", TEACHER_TG, message=card))
    await cb_students_page(cb(f"stupg:{gid}:0", TEACHER_TG, message=card))
    async with session_scope() as s:
        p = await persons_svc.get_person(s, sid, 531)
        pid = p.id

    await cb_person_card(cb(f"pcard:{gid}:{pid}", TEACHER_TG, message=card))
    assert "группы:" in card.text
    # cycle pool level and toggle gender
    await cb_person_card_level(cb(f"pcardlvl:{gid}:{pid}:x", TEACHER_TG, message=card))
    await cb_person_gender_toggle(cb(f"pcardgnd:{gid}:{pid}:f", TEACHER_TG, message=card))
    async with session_scope() as s:
        p = await persons_svc.get_person(s, sid, 531)
        assert p.gender == Gender.male  # female → male

    # invite (QR), pause, resume
    inv = cb(f"grpinv:{gid}", TEACHER_TG, message=card)
    await cb_group_invite(inv, bot)
    assert any("Приглашение" in r.text for r in card.replies)

    await cb_group_pause(cb(f"grppause:{gid}", TEACHER_TG, message=card))
    async with session_scope() as s:
        assert (await groups_svc.get_group(s, gid)).active is False
    await cb_group_resume(cb(f"grpresume:{gid}", TEACHER_TG, message=card))
    async with session_scope() as s:
        assert (await groups_svc.get_group(s, gid)).active is True

    # remove the student from the group (confirm → ok)
    await cb_person_remove_confirm(cb(f"pcardrem:{gid}:{pid}", TEACHER_TG, message=card))
    assert "Убрать" in card.text
    await cb_person_remove(cb(f"pcardremok:{gid}:{pid}", TEACHER_TG, message=card))
    async with session_scope() as s:
        assert await persons_svc.count_members(s, gid) == 0


async def test_walk_in_person_add(db, monkeypatch) -> None:
    bot = FakeBot()
    gid, sid, code = await make_group()
    now = LESSON - timedelta(hours=4)
    monkeypatch.setattr(access_mod, "utc_now", lambda: now)
    # a school person who is NOT in this group (pool-only)
    async with session_scope() as s:
        t = await admins_svc.get_by_tg_id(s, TEACHER_TG)
        person = await persons_svc.ensure_person(s, sid, 540, "u", "Гость")
        await persons_svc.set_gender(s, person.id, Gender.male)
        await persons_svc.set_pool_level(s, t.id, person.id, 1)
        pid = person.id

    card = SentMessage(bot, TEACHER_TG, "")
    await cb_person_card(cb(f"pcard:{gid}:{pid}", TEACHER_TG, message=card))
    # card offers "Записать постоянно" → add to group
    await cb_person_add_to_group(cb(f"pcardadd:{gid}:{pid}", TEACHER_TG, message=card))
    async with session_scope() as s:
        assert await persons_svc.get_in_group(s, gid, 540) is not None


async def test_ping_pending(db) -> None:
    bot = FakeBot()
    gid, sid, code = await make_group()
    for tg, name, g in ((551, "Аня", Gender.female), (552, "Боря", Gender.male)):
        await join_via_link(bot, code, tg, name, g)
    # open survey, nobody answered → everyone pending
    async with session_scope() as s:
        survey, _ = await surveys_svc.get_or_create_survey(
            s, gid, SurveyKind.attendance, LESSON
        )
        members = await persons_svc.group_members(s, gid)
        await surveys_svc.ensure_responses(s, survey.id, [p.id for p in members])
        survey_id = survey.id

    ping = cb(f"ping:{survey_id}", TEACHER_TG, message=SentMessage(bot, TEACHER_TG, ""))
    await cb_ping_pending(ping, bot)
    assert any("Пингнул" in a[0] for a in ping.answers)
    assert bot.sent_to(551) and bot.sent_to(552)  # both got the re-ask


async def test_ping_pending_double_tap_is_throttled(db) -> None:
    """Повторный тап «напомнить» сразу после первого не должен задваивать
    рассылку студентам — cooldown 60с на survey_id."""
    bot = FakeBot()
    gid, sid, code = await make_group()
    for tg, name, g in ((551, "Аня", Gender.female), (552, "Боря", Gender.male)):
        await join_via_link(bot, code, tg, name, g)
    async with session_scope() as s:
        survey, _ = await surveys_svc.get_or_create_survey(
            s, gid, SurveyKind.attendance, LESSON
        )
        members = await persons_svc.group_members(s, gid)
        await surveys_svc.ensure_responses(s, survey.id, [p.id for p in members])
        survey_id = survey.id

    ping1 = cb(f"ping:{survey_id}", TEACHER_TG, message=SentMessage(bot, TEACHER_TG, ""))
    await cb_ping_pending(ping1, bot)
    before_551 = len(bot.sent_to(551))

    ping2 = cb(f"ping:{survey_id}", TEACHER_TG, message=SentMessage(bot, TEACHER_TG, ""))
    await cb_ping_pending(ping2, bot)
    assert any("подожди" in a[0].lower() for a in ping2.answers)
    assert len(bot.sent_to(551)) == before_551  # второй тап никого не пингует


async def test_ping_pending_skips_undelivered(db) -> None:
    """Недостижимый (tg_message_id=-1) не получает повторный опрос — его
    некому пинговать (заблокировал бота/не нажал Start)."""
    bot = FakeBot()
    gid, sid, code = await make_group()
    for tg, name, g in ((551, "Аня", Gender.female), (552, "Боря", Gender.male)):
        await join_via_link(bot, code, tg, name, g)
    async with session_scope() as s:
        survey, _ = await surveys_svc.get_or_create_survey(
            s, gid, SurveyKind.attendance, LESSON
        )
        members = await persons_svc.group_members(s, gid)
        await surveys_svc.ensure_responses(s, survey.id, [p.id for p in members])
        survey_id = survey.id
        undeliverable = next(p for p in members if p.tg_user_id == 552)
        await surveys_svc.set_message_id(s, survey_id, undeliverable.id, -1)

    before_551, before_552 = len(bot.sent_to(551)), len(bot.sent_to(552))
    ping = cb(f"ping:{survey_id}", TEACHER_TG, message=SentMessage(bot, TEACHER_TG, ""))
    await cb_ping_pending(ping, bot)
    assert len(bot.sent_to(551)) > before_551  # Аня, реально молчащая, получила пинг
    assert len(bot.sent_to(552)) == before_552  # Боря недостижим — не пингуем


async def test_group_responsible_admins(db) -> None:
    bot = FakeBot()
    gid, sid, code = await make_group()
    # add a co-admin who can be toggled as responsible
    async with session_scope() as s:
        co = await admins_svc.get_or_create(s, 560, "co", "Соадмин")
        await schools_svc.add_coadmin(s, sid, co.id)
        co_id = co.id

    card = SentMessage(bot, TEACHER_TG, "")
    await cb_group_admins(cb(f"grpadm:{gid}", TEACHER_TG, message=card))
    assert "Ответственные" in card.text
    await cb_group_admin_toggle(cb(f"grpadmtog:{gid}:{co_id}", TEACHER_TG, message=card))
    async with session_scope() as s:
        assert co_id in await groups_svc.group_admin_ids(s, gid)
    # toggle off again
    await cb_group_admin_toggle(cb(f"grpadmtog:{gid}:{co_id}", TEACHER_TG, message=card))
    async with session_scope() as s:
        assert co_id not in await groups_svc.group_admin_ids(s, gid)


async def test_my_groups_list(db) -> None:
    bot = FakeBot()
    gid, sid, code = await make_group()
    # no responsible group yet → empty hint
    empty = FakeMessage(fake_user(TEACHER_TG), bot=bot)
    await cmd_groups(empty)
    assert "нет своих групп" in empty.replies[0].text

    async with session_scope() as s:
        t = await admins_svc.get_by_tg_id(s, TEACHER_TG)
        await groups_svc.set_group_admin(s, gid, t.id, True)

    populated = FakeMessage(fake_user(TEACHER_TG), bot=bot)
    await cmd_groups(populated)
    assert "Мои группы" in populated.replies[0].text

    list_msg = populated.replies[0]
    await cb_groups_list(cb("grplist", TEACHER_TG, message=list_msg))
    assert "Мои группы" in list_msg.text


# ── search ───────────────────────────────────────────────────────────


async def test_search_flows(db) -> None:
    bot = FakeBot()
    gid, sid, code = await make_group()
    for tg, name, g in (
        (571, "Аня Смирнова", Gender.female),
        (572, "Борис Орлов", Gender.male),
    ):
        await join_via_link(bot, code, tg, name, g)
    async with session_scope() as s:
        t = await admins_svc.get_by_tg_id(s, TEACHER_TG)
        for tg in (571, 572):
            p = await persons_svc.get_person(s, sid, tg)
            await persons_svc.set_pool_level(s, t.id, p.id, 1)
        survey, _ = await surveys_svc.get_or_create_survey(
            s, gid, SurveyKind.attendance, LESSON
        )
        survey_id = survey.id

    # pool search
    state = FakeFSM()
    await cb_search_start(
        cb("search:pool", TEACHER_TG, message=SentMessage(bot, TEACHER_TG, "")), state
    )
    assert state.state == EntitySearch.query
    pool_q = FakeMessage(fake_user(TEACHER_TG), text="Аня", bot=bot)
    await on_search_query(pool_q, state)
    assert any("Аня" in r.text for r in pool_q.replies)

    # students search
    state2 = FakeFSM()
    await cb_search_start(
        cb(f"search:students:{gid}", TEACHER_TG, message=SentMessage(bot, TEACHER_TG, "")), state2
    )
    stud_q = FakeMessage(fake_user(TEACHER_TG), text="Борис", bot=bot)
    await on_search_query(stud_q, state2)
    assert any("Борис" in r.text for r in stud_q.replies)

    # walk-in search (nobody matches "zzz" → "никого")
    state3 = FakeFSM()
    wk_data = f"search:walkin:{survey_id}:{gid}"
    await cb_search_start(
        cb(wk_data, TEACHER_TG, message=SentMessage(bot, TEACHER_TG, "")), state3
    )
    wk_q = FakeMessage(fake_user(TEACHER_TG), text="Аня", bot=bot)
    await on_search_query(wk_q, state3)
    assert wk_q.replies  # rendered either matches or "никого"

    # cancelled search (text starts with /)
    state4 = FakeFSM()
    await cb_search_start(
        cb("search:pool", TEACHER_TG, message=SentMessage(bot, TEACHER_TG, "")), state4
    )
    cancel_q = FakeMessage(fake_user(TEACHER_TG), text="/cancel", bot=bot)
    await on_search_query(cancel_q, state4)
    assert any("отмен" in r.text.lower() for r in cancel_q.replies)
