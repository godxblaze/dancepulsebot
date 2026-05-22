"""Tests for support recruitment — deficit detection, confirm, broadcast, claims."""
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.bot.handlers.recruit import (
    broadcast_support_call,
    cb_support_confirm_no,
    cb_support_confirm_yes,
    cb_support_join,
)
from app.bot.handlers.teacher import cb_scope_toggle
from app.db.models import (
    Answer,
    Gender,
    Person,
    SupportCallStatus,
    SupportScope,
    SurveyKind,
    Teacher,
)
from app.db.session import session_scope
from app.scheduler import scheduler as sched
from app.services import groups, persons, supports, surveys, teachers

LESSON = datetime(2026, 5, 25, 19, 0)


class FakeCbMessage:
    def __init__(self) -> None:
        self.text: str | None = None

    async def edit_text(self, text, **kwargs) -> None:
        self.text = text

    async def edit_reply_markup(self, **kwargs) -> None:
        pass


class FakeCallback:
    def __init__(self, data: str, user_id: int) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeCbMessage()
        self.answers: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs) -> None:
        self.sent.append((chat_id, text))


async def _imbalanced_lesson(males_yes: int = 1, females_yes: int = 4):
    """Group with a survey where the given numbers of each gender said 'yes'.
    Returns the Group object and the survey id."""
    async with session_scope() as s:
        t = await teachers.get_or_create(s, 100, "teach", "Препод")
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
        t = await teachers.get_or_create(s, 100, "t", "T")
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


async def test_support_candidates_pool_order_and_exclusions(db) -> None:
    async with session_scope() as s:
        t = await teachers.get_or_create(s, 100, "t", "T")
        g = await groups.create_group(s, t.id, "Бачата", "0", "19:00", 18)
        other = await groups.create_group(s, t.id, "Другая", "2", "20:00", 18)
        # male member of the target group, even in the pool — must be excluded
        member, _ = await persons.add_to_group(s, g, 1, "u", "Участник")
        await persons.set_gender(s, member.id, Gender.male)
        await persons.set_teacher_pool(s, member.id, True)
        # male in the teacher pool
        tp, _ = await persons.add_to_group(s, other, 2, "u", "Тренерский")
        await persons.set_gender(s, tp.id, Gender.male)
        await persons.set_teacher_pool(s, tp.id, True)
        # male in the open pool
        op, _ = await persons.add_to_group(s, other, 3, "u", "Открытый")
        await persons.set_gender(s, op.id, Gender.male)
        await persons.set_open_pool(s, op.id, True)
        # female in the teacher pool — wrong gender
        fp, _ = await persons.add_to_group(s, other, 4, "u", "Девушка")
        await persons.set_gender(s, fp.id, Gender.female)
        await persons.set_teacher_pool(s, fp.id, True)
        tid, gid, tp_id, op_id = t.id, g.id, tp.id, op.id
    async with session_scope() as s:
        both = await persons.support_candidates(s, tid, Gender.male, gid, True)
        assert [p.id for p in both] == [tp_id, op_id]  # teacher pool first
        own = await persons.support_candidates(s, tid, Gender.male, gid, False)
        assert [p.id for p in own] == [tp_id]  # open pool excluded


# ── supports.claim ───────────────────────────────────────────────────


async def _open_call(needed: int) -> tuple[int, list[int]]:
    async with session_scope() as s:
        t = await teachers.get_or_create(s, 100, "t", "T")
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
    assert bot.sent == []  # teacher not bothered


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


# ── recruit handlers ─────────────────────────────────────────────────


async def _pending_call_with_support() -> int:
    async with session_scope() as s:
        t = await teachers.get_or_create(s, 100, "t", "Препод")
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
        await persons.set_teacher_pool(s, sup.id, True)
        return call.id


async def test_confirm_yes_opens_and_broadcasts(db) -> None:
    cid = await _pending_call_with_support()
    bot = FakeBot()
    await cb_support_confirm_yes(FakeCallback(f"sup:yes:{cid}", user_id=100), bot)
    async with session_scope() as s:
        call = await supports.get_call(s, cid)
        assert call is not None and call.status == SupportCallStatus.open
    assert any(chat == 700 for chat, _ in bot.sent)  # support got the broadcast


async def test_confirm_no_closes(db) -> None:
    cid = await _pending_call_with_support()
    await cb_support_confirm_no(FakeCallback(f"sup:no:{cid}", user_id=100))
    async with session_scope() as s:
        call = await supports.get_call(s, cid)
        assert call is not None and call.status == SupportCallStatus.closed


async def test_confirm_rejects_non_teacher(db) -> None:
    cid = await _pending_call_with_support()
    bot = FakeBot()
    # someone who is not the teacher must not be able to confirm the call
    await cb_support_confirm_yes(FakeCallback(f"sup:yes:{cid}", user_id=999), bot)
    async with session_scope() as s:
        call = await supports.get_call(s, cid)
        assert call is not None and call.status == SupportCallStatus.pending
    assert bot.sent == []


async def test_broadcast_empty_pool_notifies_teacher(db) -> None:
    async with session_scope() as s:
        t = await teachers.get_or_create(s, 100, "t", "Препод")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, LESSON
        )
        call = await supports.create_call(
            s, survey.id, Gender.male, 2, SupportCallStatus.open
        )
        cid = call.id
    bot = FakeBot()
    await broadcast_support_call(bot, cid)
    assert any(chat == 100 and "вручную" in text for chat, text in bot.sent)


async def test_support_join_records_claim_and_notifies_teacher(db) -> None:
    cid = await _pending_call_with_support()
    async with session_scope() as s:
        await supports.set_status(s, cid, SupportCallStatus.open)
    bot = FakeBot()
    await cb_support_join(FakeCallback(f"sup:join:{cid}", user_id=700), bot)
    async with session_scope() as s:
        assert await supports.count_claims(s, cid) == 1
    assert any(chat == 100 for chat, _ in bot.sent)  # teacher notified


# ── scope toggle ─────────────────────────────────────────────────────


async def test_scope_toggle(db) -> None:
    async with session_scope() as s:
        t = await teachers.get_or_create(s, 100, "t", "T")
        tid = t.id
        assert t.support_scope == SupportScope.own_and_open  # default
    await cb_scope_toggle(FakeCallback("sscope:toggle", user_id=100))
    async with session_scope() as s:
        assert (await s.get(Teacher, tid)).support_scope == SupportScope.own
    await cb_scope_toggle(FakeCallback("sscope:toggle", user_id=100))
    async with session_scope() as s:
        assert (await s.get(Teacher, tid)).support_scope == SupportScope.own_and_open
