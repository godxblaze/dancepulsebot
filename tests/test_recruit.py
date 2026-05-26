"""Tests for support recruitment — deficit detection, the picker, claims."""
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.bot.handlers.recruit import (
    cb_support_join,
    cb_support_pick,
    cb_support_pick_send,
    cb_support_skip,
)
from app.bot.keyboards import support_pick_keyboard
from app.db.models import Answer, Gender, Person, SupportCallStatus, SurveyKind
from app.db.session import session_scope
from app.scheduler import scheduler as sched
from app.services import admins, groups, persons, supports, surveys

LESSON = datetime(2026, 5, 25, 19, 0)


class FakeCbMessage:
    def __init__(self, reply_markup=None) -> None:
        self.text: str | None = None
        self.reply_markup = reply_markup

    async def edit_text(self, text, **kwargs) -> None:
        self.text = text

    async def edit_reply_markup(self, reply_markup=None, **kwargs) -> None:
        self.reply_markup = reply_markup


class FakeCallback:
    def __init__(self, data: str, user_id: int, reply_markup=None) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeCbMessage(reply_markup)
        self.answers: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs) -> None:
        self.sent.append((chat_id, text))


async def _imbalanced_lesson(males_yes: int = 1, females_yes: int = 4):
    """Group + survey where the given numbers of each gender said 'yes'."""
    async with session_scope() as s:
        t = await admins.get_or_create(s, 100, "teach", "Препод")
        g = await groups.create_group(s, t.id, "Бачата", "0", "19:00", 18)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, LESSON
        )
        uid = 200
        for gender, count in ((Gender.male, males_yes), (Gender.female, females_yes)):
            for _ in range(count):
                p, _ = await persons.add_to_group(s, g, uid, "u", f"P{uid}")
                await persons.set_gender(s, p.id, gender)
                await surveys.record_answer(s, survey.id, p.id, Answer.yes)
                uid += 1
        return g, survey.id


# ── gender_yes_counts ────────────────────────────────────────────────


async def test_gender_yes_counts(db) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 100, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, LESSON
        )
        for uid, gender, ans in (
            (1, Gender.male, Answer.yes),
            (2, Gender.male, Answer.yes),
            (3, Gender.female, Answer.yes),
            (4, Gender.female, Answer.no),
        ):
            p, _ = await persons.add_to_group(s, g, uid, "u", f"P{uid}")
            await persons.set_gender(s, p.id, gender)
            await surveys.record_answer(s, survey.id, p.id, ans)
        sid = survey.id
    async with session_scope() as s:
        assert await surveys.gender_yes_counts(s, sid) == (2, 1)


# ── support_candidates ───────────────────────────────────────────────


async def test_support_candidates_pool_gender_level(db) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 100, "t", "T")
        g = await groups.create_group(s, t.id, "Бачата", "0", "19:00", 18)
        other = await groups.create_group(s, t.id, "Другая", "2", "20:00", 18)
        # male member of the target group, even in the pool — excluded
        member, _ = await persons.add_to_group(s, g, 1, "u", "Участник")
        await persons.set_gender(s, member.id, Gender.male)
        await persons.set_support_level(s, member.id, 3)
        # male pool members, not in the target group
        lo, _ = await persons.add_to_group(s, other, 2, "u", "Слабый")
        await persons.set_gender(s, lo.id, Gender.male)
        await persons.set_support_level(s, lo.id, 1)
        hi, _ = await persons.add_to_group(s, other, 3, "u", "Сильный")
        await persons.set_gender(s, hi.id, Gender.male)
        await persons.set_support_level(s, hi.id, 3)
        # male not in the pool — excluded
        out, _ = await persons.add_to_group(s, other, 4, "u", "Вне пула")
        await persons.set_gender(s, out.id, Gender.male)
        # female in the pool — wrong gender
        fp, _ = await persons.add_to_group(s, other, 5, "u", "Девушка")
        await persons.set_gender(s, fp.id, Gender.female)
        await persons.set_support_level(s, fp.id, 2)
        tid, gid, lo_id, hi_id = t.id, g.id, lo.id, hi.id
    async with session_scope() as s:
        cands = await persons.support_candidates(s, tid, Gender.male, gid)
    # only the two male pool members not in the group, highest level first
    assert [p.id for p in cands] == [hi_id, lo_id]


# ── supports.claim ───────────────────────────────────────────────────


async def _open_call(needed: int) -> tuple[int, list[int]]:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 100, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, LESSON
        )
        call = await supports.create_call(
            s, survey.id, Gender.male, needed, SupportCallStatus.open
        )
        ids = []
        for uid in range(201, 205):
            p, _ = await persons.add_to_group(s, g, uid, "u", f"P{uid}")
            ids.append(p.id)
        return call.id, ids


async def test_claim_fills_and_closes(db) -> None:
    cid, pids = await _open_call(needed=2)
    async with session_scope() as s:
        assert await supports.claim(s, cid, pids[0]) == (True, 1, 2)
    async with session_scope() as s:
        assert await supports.claim(s, cid, pids[1]) == (True, 2, 2)
    async with session_scope() as s:
        accepted, filled, _ = await supports.claim(s, cid, pids[2])
        assert not accepted and filled == 2  # full
    async with session_scope() as s:
        call = await supports.get_call(s, cid)
        assert call is not None and call.status == SupportCallStatus.closed
        assert (await s.get(Person, pids[0])).helped_count == 1
        assert (await s.get(Person, pids[2])).helped_count == 0


async def test_claim_duplicate_not_double_counted(db) -> None:
    cid, pids = await _open_call(needed=3)
    async with session_scope() as s:
        assert (await supports.claim(s, cid, pids[0]))[0] is True
    async with session_scope() as s:
        accepted, filled, _ = await supports.claim(s, cid, pids[0])  # same person
        assert not accepted and filled == 1
    async with session_scope() as s:
        assert (await s.get(Person, pids[0])).helped_count == 1


# ── scheduler _maybe_recruit ─────────────────────────────────────────


async def test_maybe_recruit_creates_pending_call(db) -> None:
    g, survey_id = await _imbalanced_lesson(males_yes=1, females_yes=4)
    bot = FakeBot()
    await sched._maybe_recruit(bot, g, LESSON, LESSON - timedelta(hours=3))
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, survey_id)
        assert call is not None
        assert call.status == SupportCallStatus.pending
        assert call.gender == Gender.male and call.needed == 3
    assert any(chat == 100 for chat, _ in bot.sent)  # teacher asked


async def test_maybe_recruit_below_threshold_closes(db) -> None:
    g, survey_id = await _imbalanced_lesson(males_yes=3, females_yes=4)  # deficit 1
    bot = FakeBot()
    await sched._maybe_recruit(bot, g, LESSON, LESSON - timedelta(hours=3))
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, survey_id)
        assert call is not None and call.status == SupportCallStatus.closed
    assert bot.sent == []


async def test_maybe_recruit_before_cutoff_does_nothing(db) -> None:
    g, survey_id = await _imbalanced_lesson()
    bot = FakeBot()
    await sched._maybe_recruit(bot, g, LESSON, LESSON - timedelta(hours=10))
    async with session_scope() as s:
        assert await supports.get_call_for_survey(s, survey_id) is None


async def test_maybe_recruit_idempotent(db) -> None:
    g, _ = await _imbalanced_lesson()
    bot = FakeBot()
    now = LESSON - timedelta(hours=3)
    await sched._maybe_recruit(bot, g, LESSON, now)
    await sched._maybe_recruit(bot, g, LESSON, now)
    assert len(bot.sent) == 1  # asked only once


# ── recruit picker ───────────────────────────────────────────────────


async def _call_with_pool_candidate():
    async with session_scope() as s:
        t = await admins.get_or_create(s, 100, "teach", "Препод")
        g = await groups.create_group(s, t.id, "Бачата", "0", "19:00", 18)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, LESSON
        )
        call = await supports.create_call(
            s, survey.id, Gender.male, 2, SupportCallStatus.pending
        )
        other = await groups.create_group(s, t.id, "Другая", "2", "20:00", 18)
        sup, _ = await persons.add_to_group(s, other, 700, "u", "Саппорт")
        await persons.set_gender(s, sup.id, Gender.male)
        await persons.set_support_level(s, sup.id, 2)
        return SimpleNamespace(
            call_id=call.id, sup_pid=sup.id, admin_id=t.id, group_id=g.id
        )


async def test_pick_and_send_invites(db) -> None:
    ctx = await _call_with_pool_candidate()
    async with session_scope() as s:
        candidates = await persons.support_candidates(
            s, ctx.admin_id, Gender.male, ctx.group_id
        )
    kb = support_pick_keyboard(ctx.call_id, candidates, set())
    pick = FakeCallback(
        f"pick:{ctx.call_id}:{ctx.sup_pid}", user_id=100, reply_markup=kb
    )
    await cb_support_pick(pick)  # tick the support

    bot = FakeBot()
    send = FakeCallback(
        f"picksend:{ctx.call_id}", user_id=100, reply_markup=pick.message.reply_markup
    )
    await cb_support_pick_send(send, bot)
    assert any(chat == 700 for chat, _ in bot.sent)  # support invited
    async with session_scope() as s:
        call = await supports.get_call(s, ctx.call_id)
        assert call is not None and call.status == SupportCallStatus.open


async def test_pick_send_nothing_selected(db) -> None:
    ctx = await _call_with_pool_candidate()
    async with session_scope() as s:
        candidates = await persons.support_candidates(
            s, ctx.admin_id, Gender.male, ctx.group_id
        )
    kb = support_pick_keyboard(ctx.call_id, candidates, set())  # nobody ticked
    bot = FakeBot()
    send = FakeCallback(f"picksend:{ctx.call_id}", user_id=100, reply_markup=kb)
    await cb_support_pick_send(send, bot)
    assert bot.sent == []  # no invites
    assert send.answers and "отметь" in send.answers[0].lower()


async def test_pick_send_rejects_foreign_teacher(db) -> None:
    ctx = await _call_with_pool_candidate()
    async with session_scope() as s:
        await admins.get_or_create(s, 999, "x", "Чужой Препод")
        candidates = await persons.support_candidates(
            s, ctx.admin_id, Gender.male, ctx.group_id
        )
    kb = support_pick_keyboard(ctx.call_id, candidates, {ctx.sup_pid})
    bot = FakeBot()
    send = FakeCallback(f"picksend:{ctx.call_id}", user_id=999, reply_markup=kb)
    await cb_support_pick_send(send, bot)
    assert bot.sent == []  # foreign teacher cannot invite
    async with session_scope() as s:
        call = await supports.get_call(s, ctx.call_id)
        assert call is not None and call.status == SupportCallStatus.pending


async def test_support_join_records_claim(db) -> None:
    ctx = await _call_with_pool_candidate()
    async with session_scope() as s:
        await supports.set_status(s, ctx.call_id, SupportCallStatus.open)
    bot = FakeBot()
    await cb_support_join(FakeCallback(f"sup:join:{ctx.call_id}", user_id=700), bot)
    async with session_scope() as s:
        assert await supports.count_claims(s, ctx.call_id) == 1
    assert any(chat == 100 for chat, _ in bot.sent)  # teacher notified


async def test_support_flip_skip_after_join(db) -> None:
    """After saying yes a support can flip to «не смогу» — claim is reverted
    and a closed call is re-opened. Teacher is told about the freed slot."""
    ctx = await _call_with_pool_candidate()
    async with session_scope() as s:
        await supports.set_status(s, ctx.call_id, SupportCallStatus.open)
        # needed=2; lower it to 1 so a single claim closes the call
        call = await supports.get_call(s, ctx.call_id)
        call.needed = 1

    bot = FakeBot()
    await cb_support_join(FakeCallback(f"sup:join:{ctx.call_id}", user_id=700), bot)
    async with session_scope() as s:
        c = await supports.get_call(s, ctx.call_id)
        assert c.status == SupportCallStatus.closed
        assert await supports.count_claims(s, ctx.call_id) == 1

    skip_cb = FakeCallback(f"sup:skip:{ctx.call_id}", user_id=700)
    await cb_support_skip(skip_cb, bot)
    async with session_scope() as s:
        c = await supports.get_call(s, ctx.call_id)
        assert c.status == SupportCallStatus.open  # slot freed
        assert await supports.count_claims(s, ctx.call_id) == 0
    # teacher gets a heads-up
    assert any("передумал" in t for _, t in bot.sent)
    # ack message has the flip-back button
    assert skip_cb.message.text and "не сможешь" in skip_cb.message.text


async def test_support_flip_skip_without_prior_claim(db) -> None:
    """Skip without a prior join is still a clean ack — no teacher ping."""
    ctx = await _call_with_pool_candidate()
    async with session_scope() as s:
        await supports.set_status(s, ctx.call_id, SupportCallStatus.open)
    bot = FakeBot()
    cb = FakeCallback(f"sup:skip:{ctx.call_id}", user_id=700)
    await cb_support_skip(cb, bot)
    assert bot.sent == []  # teacher not pinged
    assert cb.message.text and "не сможешь" in cb.message.text
