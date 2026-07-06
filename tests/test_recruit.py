"""Tests for support recruitment — deficit detection, the picker, claims."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest_asyncio
from sqlalchemy import select

from app.bot.handlers.recruit import (
    cb_support_join,
    cb_support_pick,
    cb_support_pick_nope,
    cb_support_pick_send,
    cb_support_pickmore,
    cb_support_skip,
)
from app.bot.keyboards import support_pick_keyboard
from app.db.models import Answer, Gender, Person, SupportCallStatus, SurveyKind
from app.db.session import engine, session_scope
from app.scheduler import scheduler as sched
from app.services import admins, groups, persons, schools, supports, surveys


async def _drain_pool() -> None:
    """Закрыть пулованные aiosqlite-соединения после теста с реальной
    конкуренцией (asyncio.gather): их WAL-локи на общей файловой тест-БД иначе
    роняют следующий тест «database is locked»."""
    await engine.dispose()


@pytest_asyncio.fixture
async def drain_pool_after():
    """То же самое, но как fixture-teardown — гарантированно отрабатывает,
    даже если тест упал на assert'е ДО явного вызова _drain_pool()."""
    yield
    await _drain_pool()

_MSK = ZoneInfo("Europe/Moscow")
# Занятие в будущем (cb_support_join сверяет lesson_at с реальным utc_now —
# фикс-дата в прошлом ломала бы guard «добор после начала занятия»). 19:00 МСК
# через 14 дней: -30 ч заведомо раньше вечернего разбора, -3 ч — позже.
LESSON = (
    (datetime.now(UTC).astimezone(_MSK) + timedelta(days=14))
    .replace(hour=19, minute=0, second=0, microsecond=0)
    .astimezone(UTC)
)


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
        self.edited: list[tuple[int, int]] = []  # (chat_id, message_id) — фиксирует edit-вызовы

    async def send_message(self, chat_id, text, **kwargs) -> None:
        self.sent.append((chat_id, text))

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs) -> None:
        self.edited.append((chat_id, message_id))


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
        await persons.set_pool_level(s, t.id, member.id, 3)
        # male pool members, not in the target group
        lo, _ = await persons.add_to_group(s, other, 2, "u", "Слабый")
        await persons.set_gender(s, lo.id, Gender.male)
        await persons.set_pool_level(s, t.id, lo.id, 1)
        hi, _ = await persons.add_to_group(s, other, 3, "u", "Сильный")
        await persons.set_gender(s, hi.id, Gender.male)
        await persons.set_pool_level(s, t.id, hi.id, 3)
        # male not in the pool — excluded
        out, _ = await persons.add_to_group(s, other, 4, "u", "Вне пула")
        await persons.set_gender(s, out.id, Gender.male)
        # female in the pool — wrong gender
        fp, _ = await persons.add_to_group(s, other, 5, "u", "Девушка")
        await persons.set_gender(s, fp.id, Gender.female)
        await persons.set_pool_level(s, t.id, fp.id, 2)
        tid, gid, lo_id, hi_id = t.id, g.id, lo.id, hi.id
    async with session_scope() as s:
        cands = await persons.support_candidates(s, tid, Gender.male, gid)
    # only the two male pool members not in the group, highest level first
    assert [p.id for p in cands] == [hi_id, lo_id]


async def test_support_candidates_school_id_excludes_cross_school_leak(db) -> None:
    """Лид-препод нескольких школ: личный пул не должен подсовывать саппорта
    ИЗ ДРУГОЙ школы на занятие этой — SupportPoolEntry сам по себе school-агностичен."""
    async with session_scope() as s:
        t = await admins.get_or_create(s, 100, "t", "T")
        g = await groups.create_group(s, t.id, "Бачата", "0", "19:00", 18)
        home_school_id = g.school_id
        # тот же препод co-admin'ит вторую (чужую) школу — там у него тоже пул
        other_owner = await admins.get_or_create(s, 999, "o", "O")
        other_school = await schools.get_owned_school(s, other_owner.id)
        await schools.add_coadmin(s, other_school.id, t.id)
        other_group = await groups.create_group(s, other_school.id, "Сальса", "0", "18:00", 18)
        foreign, _ = await persons.add_to_group(s, other_group, 1, "u", "Чужак")
        await persons.set_gender(s, foreign.id, Gender.male)
        await persons.set_pool_level(s, t.id, foreign.id, 3)
        home_group = await groups.create_group(s, home_school_id, "Пул", "2", "20:00", 18)
        home, _ = await persons.add_to_group(s, home_group, 2, "u", "Свой")
        await persons.set_gender(s, home.id, Gender.male)
        await persons.set_pool_level(s, t.id, home.id, 1)
        tid, gid, home_id = t.id, g.id, home.id
    async with session_scope() as s:
        without_filter = await persons.support_candidates(s, tid, Gender.male, gid)
        with_filter = await persons.support_candidates(s, tid, Gender.male, gid, home_school_id)
    assert any(p.full_name == "Чужак" for p in without_filter)  # без фильтра утечка есть
    assert [p.id for p in with_filter] == [home_id]  # с фильтром — только свой


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
        # claim сам по себе helped_count НЕ инкрементит — он по факту явки.
        assert (await s.get(Person, pids[0])).helped_count == 0
        assert (await s.get(Person, pids[2])).helped_count == 0


async def test_claim_duplicate_not_double_counted(db) -> None:
    cid, pids = await _open_call(needed=3)
    async with session_scope() as s:
        assert (await supports.claim(s, cid, pids[0]))[0] is True
    async with session_scope() as s:
        accepted, filled, _ = await supports.claim(s, cid, pids[0])  # same person
        assert not accepted and filled == 1
    async with session_scope() as s:
        assert (await s.get(Person, pids[0])).helped_count == 0  # явка не отмечена


async def test_claim_rejected_on_closed_call(db) -> None:
    """Call закрыт (needed не доеден по count — напр. вручную выставленный
    closed) → claim отказывает по статусу, не только по заполненности."""
    cid, pids = await _open_call(needed=2)
    async with session_scope() as s:
        call = await supports.get_call(s, cid)
        call.status = SupportCallStatus.closed  # закрыт, хотя filled(0) < needed(2)
    async with session_scope() as s:
        accepted, filled, needed = await supports.claim(s, cid, pids[0])
        assert not accepted and filled == 0 and needed == 2
    async with session_scope() as s:
        assert await supports.count_claims(s, cid) == 0  # ничего не записалось
        call = await supports.get_call(s, cid)
        assert call.status == SupportCallStatus.closed  # статус не тронут


async def test_claim_rejected_after_dismiss(db) -> None:
    """dismiss_call() закрывает pending-call — claim по нему больше не проходит."""
    ctx = await _call_with_pool_candidate()  # pending call
    async with session_scope() as s:
        assert await supports.dismiss_call(s, ctx.call_id) is True
    async with session_scope() as s:
        accepted, filled, _ = await supports.claim(s, ctx.call_id, ctx.sup_pid)
        assert not accepted and filled == 0
    async with session_scope() as s:
        assert await supports.count_claims(s, ctx.call_id) == 0
        call = await supports.get_call(s, ctx.call_id)
        assert call.status == SupportCallStatus.closed  # dismiss не откатился


async def test_late_claim_does_not_wipe_picker_keyboard(db) -> None:
    """B: поздний claim по инвайту фазы-1 не должен снести клавиатуру
    live picker'а фазы-2 (picker_ref теперь указывает на него). _refresh_balance
    должен пропустить edit сообщения с picker_has_keyboard=True."""
    cid, _pids = await _open_call(needed=2)  # tg_user_id саппортов — 201..204
    async with session_scope() as s:
        # phase-2 переставила picker_ref на свежий picker С клавиатурой.
        await supports.set_picker_ref(s, cid, 100, 555, has_keyboard=True)
    bot = FakeBot()
    cb = FakeCallback(f"sup:join:{cid}", user_id=201)  # поздний саппорт фазы-1
    await cb_support_join(cb, bot)
    async with session_scope() as s:
        assert await supports.count_claims(s, cid) == 1  # claim прошёл
    assert bot.edited == []  # picker-сообщение (chat=100, msg=555) не редактировалось


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


async def test_maybe_recruit_before_evening_fix_does_nothing(db) -> None:
    """Фаза 1 (вечерний разбор) не срабатывает раньше своего времени накануне.
    Дефолт 20:00 МСК накануне = за ~23 ч до занятия; за 30 ч ещё рано."""
    g, survey_id = await _imbalanced_lesson()
    bot = FakeBot()
    await sched._maybe_recruit(bot, g, LESSON, LESSON - timedelta(hours=30))
    async with session_scope() as s:
        assert await supports.get_call_for_survey(s, survey_id) is None
    assert bot.sent == []


async def test_maybe_recruit_idempotent(db) -> None:
    g, _ = await _imbalanced_lesson()
    bot = FakeBot()
    now = LESSON - timedelta(hours=3)
    await sched._maybe_recruit(bot, g, LESSON, now)
    await sched._maybe_recruit(bot, g, LESSON, now)
    assert len(bot.sent) == 1  # asked only once (фаза 2 без новой дыры молчит)


async def test_topup_recruits_after_late_cancellation(db) -> None:
    """Вечером баланс сложился (closed), потом отмены создали дыру — добор
    остатка в день занятия её ловит и зовёт ещё."""
    g, survey_id = await _imbalanced_lesson(males_yes=3, females_yes=3)  # balanced
    bot = FakeBot()
    # фаза 1 на вечерней фиксации — баланс ок, call закрыт, needed=0
    await sched._maybe_recruit(bot, g, LESSON, LESSON - timedelta(hours=20))
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, survey_id)
        assert call is not None and call.status == SupportCallStatus.closed
        # две девушки отменяются → перекос 3 vs 1
        females = [
            p for p in await persons.group_members(s, g.id)
            if (await s.get(Person, p.id)).gender == Gender.female
        ]
        for p in females[:2]:
            await surveys.record_answer(s, survey_id, p.id, Answer.no)
        # в пуле lead'а (owner) есть две девушки-саппорта из соседней группы
        owner = await admins.get_by_tg_id(s, 100)
        other = await groups.create_group(s, g.school_id, "Сосед", "2", "20:00", 18)
        for uid in (901, 902):
            fp, _ = await persons.add_to_group(s, other, uid, "u", f"F{uid}")
            await persons.set_gender(s, fp.id, Gender.female)
            await persons.set_pool_level(s, owner.id, fp.id, 2)

    # фаза 2 на доборе остатка (дефолт 3 ч до занятия)
    await sched._maybe_recruit(bot, g, LESSON, LESSON - timedelta(hours=2))
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, survey_id)
        assert call.status == SupportCallStatus.pending
        assert call.gender == Gender.female and call.needed == 2
        assert call.topup_done is True
    assert any(chat == 100 for chat, _ in bot.sent)  # owner got the top-up picker


async def test_topup_disabled_when_cutoff_zero(db) -> None:
    """support_cutoff_hours=0 — фаза 2 выключена, добора остатка нет."""
    g, survey_id = await _imbalanced_lesson(males_yes=1, females_yes=4)
    async with session_scope() as s:
        owner = await admins.get_by_tg_id(s, 100)
        await admins.set_support_cutoff_hours(s, owner.id, 0)
    bot = FakeBot()
    await sched._maybe_recruit(bot, g, LESSON, LESSON - timedelta(hours=20))  # фаза 1
    sent_after_phase1 = len(bot.sent)
    await sched._maybe_recruit(bot, g, LESSON, LESSON - timedelta(hours=1))  # фаза 2 off
    assert len(bot.sent) == sent_after_phase1  # ничего нового
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, survey_id)
        assert call.topup_done is False  # фаза 2 даже не запускалась


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
        await persons.set_pool_level(s, t.id, sup.id, 2)  # пул owner'а (= lead по fallback)
        return SimpleNamespace(
            call_id=call.id, sup_pid=sup.id, admin_id=t.id,
            group_id=g.id, school_id=g.school_id,
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


async def test_pick_send_rejects_after_lesson_started(db) -> None:
    """Занятие уже началось — рассылать «Нужна помощь!» поздно (зеркало гарда
    join/skip)."""
    ctx = await _call_with_pool_candidate()
    async with session_scope() as s:
        survey = await surveys.get_survey(s, (await supports.get_call(s, ctx.call_id)).survey_id)
        survey.lesson_at = datetime.now(UTC) - timedelta(minutes=5)
        candidates = await persons.support_candidates(
            s, ctx.admin_id, Gender.male, ctx.group_id
        )
    kb = support_pick_keyboard(ctx.call_id, candidates, {ctx.sup_pid})
    bot = FakeBot()
    send = FakeCallback(f"picksend:{ctx.call_id}", user_id=100, reply_markup=kb)
    await cb_support_pick_send(send, bot)
    assert bot.sent == []
    assert send.answers and "уже началось" in send.answers[0]
    async with session_scope() as s:
        call = await supports.get_call(s, ctx.call_id)
        assert call is not None and call.status == SupportCallStatus.pending  # не тронут


async def test_pick_nope_dismisses_call(db) -> None:
    """«✗ Не сейчас · баланс ок» гасит pending-call, чтобы pending-нудж молчал."""
    ctx = await _call_with_pool_candidate()  # call в статусе pending
    cb = FakeCallback(f"picknope:{ctx.call_id}", user_id=100)
    await cb_support_pick_nope(cb)
    async with session_scope() as s:
        call = await supports.get_call(s, ctx.call_id)
        assert call is not None and call.status == SupportCallStatus.closed
    assert cb.message.text and "не нужен" in cb.message.text.lower()


async def test_pick_nope_dismiss_text_is_honest(db) -> None:
    """Dismiss терминален — ответ не обещает несуществующий путь «передумать»."""
    ctx = await _call_with_pool_candidate()
    cb = FakeCallback(f"picknope:{ctx.call_id}", user_id=100)
    await cb_support_pick_nope(cb)
    assert cb.message.text
    text = cb.message.text.lower()
    assert "позову сам" in text
    assert "передумаешь" not in text
    assert "🆘" not in cb.message.text


async def test_pick_nope_rejects_foreign_teacher(db) -> None:
    ctx = await _call_with_pool_candidate()
    async with session_scope() as s:
        await admins.get_or_create(s, 999, "x", "Чужой")
    cb = FakeCallback(f"picknope:{ctx.call_id}", user_id=999)
    await cb_support_pick_nope(cb)
    async with session_scope() as s:
        call = await supports.get_call(s, ctx.call_id)
        assert call is not None and call.status == SupportCallStatus.pending  # нетронут


async def test_dismissed_call_not_resurrected_by_topup(db) -> None:
    """«Не сейчас» терминально: фаза-2 добора остатка не воскрешает закрытый
    вручную call и не шлёт новый picker, даже если дефицит подрос."""
    g, survey_id = await _imbalanced_lesson(males_yes=1, females_yes=4)  # дефицит 3
    bot = FakeBot()
    await sched._maybe_recruit(bot, g, LESSON, LESSON - timedelta(hours=20))  # фаза 1
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, survey_id)
        assert call is not None and call.status == SupportCallStatus.pending
        assert await supports.dismiss_call(s, call.id) is True
        # дефицит растёт: ещё три девушки говорят «приду» → перекос 1 vs 7
        uid = 9000
        for _ in range(3):
            p, _ = await persons.add_to_group(s, g, uid, "u", f"F{uid}")
            await persons.set_gender(s, p.id, Gender.female)
            await surveys.record_answer(s, survey_id, p.id, Answer.yes)
            uid += 1
    sent_before = len(bot.sent)
    await sched._maybe_recruit(bot, g, LESSON, LESSON - timedelta(hours=2))  # фаза 2
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, survey_id)
        assert call.status == SupportCallStatus.closed  # не воскрешён
    assert len(bot.sent) == sent_before  # нового picker'а нет


async def test_pick_nope_on_already_handled_call(db) -> None:
    """Call уже разобран (open) — «не сейчас» не закрывает и сообщает об этом."""
    ctx = await _call_with_pool_candidate()
    async with session_scope() as s:
        await supports.set_status(s, ctx.call_id, SupportCallStatus.open)
    cb = FakeCallback(f"picknope:{ctx.call_id}", user_id=100)
    await cb_support_pick_nope(cb)
    async with session_scope() as s:
        assert (await supports.get_call(s, ctx.call_id)).status == SupportCallStatus.open
    assert cb.answers and "разобрано" in cb.answers[0].lower()


async def test_picksend_and_picknope_concurrent_consistent(db, drain_pool_after) -> None:
    """picksend и picknope на одном call сериализуются call_lock'ом: исход
    согласован — либо инвайты разосланы и call open, либо dismissed без инвайтов.
    Никогда «инвайты ушли, но call закрыт как ‘баланс ок’»."""
    ctx = await _call_with_pool_candidate()  # pending call, саппорт 700 в пуле lead'а
    async with session_scope() as s:
        cands = await persons.support_candidates(
            s, ctx.admin_id, Gender.male, ctx.group_id
        )
    kb = support_pick_keyboard(ctx.call_id, cands, {ctx.sup_pid})
    bot = FakeBot()
    send = FakeCallback(f"picksend:{ctx.call_id}", user_id=100, reply_markup=kb)
    nope = FakeCallback(f"picknope:{ctx.call_id}", user_id=100)
    await asyncio.gather(
        cb_support_pick_send(send, bot),
        cb_support_pick_nope(nope),
    )
    async with session_scope() as s:
        call = await supports.get_call(s, ctx.call_id)
    invited = any(chat == 700 for chat, _ in bot.sent)
    if call.status == SupportCallStatus.closed:
        assert not invited  # dismiss выиграл → инвайтов быть не должно
    else:
        assert call.status == SupportCallStatus.open and invited  # picksend выиграл


async def test_support_join_no_overfill_under_concurrency(db, drain_pool_after) -> None:
    """Два одновременных «Приду помочь» на единственный слот: per-call
    asyncio.Lock сериализует read→claim→commit, набор не перебирается."""
    async with session_scope() as s:
        t = await admins.get_or_create(s, 100, "teach", "Препод")
        g = await groups.create_group(s, t.id, "Бачата", "0", "19:00", 18)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, LESSON
        )
        call = await supports.create_call(
            s, survey.id, Gender.male, 1, SupportCallStatus.open
        )
        for uid in (700, 701):
            p, _ = await persons.add_to_group(s, g, uid, "u", f"P{uid}")
            await persons.set_gender(s, p.id, Gender.male)
        call_id = call.id
    bot = FakeBot()
    await asyncio.gather(
        cb_support_join(FakeCallback(f"sup:join:{call_id}", user_id=700), bot),
        cb_support_join(FakeCallback(f"sup:join:{call_id}", user_id=701), bot),
    )
    async with session_scope() as s:
        assert await supports.count_claims(s, call_id) == 1  # ровно один слот
        assert (await supports.get_call(s, call_id)).status == SupportCallStatus.closed


async def test_join_and_skip_concurrent_no_stuck_closed(db, drain_pool_after) -> None:
    """Конкурентные join (заполняет последний слот → closed) и skip (unclaim)
    сериализуются call_lock'ом: call никогда не остаётся closed со свободным
    слотом — unclaim видит закоммиченный closed и переоткрывает."""
    ctx = await _call_with_pool_candidate()  # needed=2, саппорт 700 (A) в пуле lead'а
    async with session_scope() as s:
        await supports.set_status(s, ctx.call_id, SupportCallStatus.open)
        g = await groups.get_group(s, ctx.group_id)
        b, _ = await persons.add_to_group(s, g, 701, "u", "B")
        await persons.set_gender(s, b.id, Gender.male)
        await supports.claim(s, ctx.call_id, ctx.sup_pid)  # A записан → count=1/2, open
    bot = FakeBot()
    await asyncio.gather(
        cb_support_join(FakeCallback(f"sup:join:{ctx.call_id}", user_id=701), bot),  # B
        cb_support_skip(FakeCallback(f"sup:skip:{ctx.call_id}", user_id=700), bot),  # A
    )
    async with session_scope() as s:
        call = await supports.get_call(s, ctx.call_id)
        count = await supports.count_claims(s, ctx.call_id)
    # инвариант: нет «закрыт, но слот свободен»
    assert not (call.status == SupportCallStatus.closed and count < call.needed)


async def test_unclaim_does_not_reopen_call_closed_below_capacity(db) -> None:
    """closed НЕ от заполнения нормы (dismiss или phase-2 «баланс сложился» при
    filled<needed) — сознательное решение, а не «слот освободился». unclaim
    не должен воскрешать такой call, иначе новый саппорт сможет застолбить
    слот на call, который лид/система уже закрыли."""
    ctx = await _call_with_pool_candidate()  # needed=2
    async with session_scope() as s:
        await supports.set_status(s, ctx.call_id, SupportCallStatus.open)
        await supports.claim(s, ctx.call_id, ctx.sup_pid)  # filled=1/2, ещё open
        # Симулируем phase-2: баланс сложился (поздние отмены с другой стороны),
        # call закрывается НЕСМОТРЯ на filled(1) < needed(2) — «не сейчас».
        call = await supports.get_call(s, ctx.call_id)
        call.status = SupportCallStatus.closed
        await s.flush()
    async with session_scope() as s:
        changed = await supports.unclaim(
            s, ctx.call_id, ctx.sup_pid, now=LESSON - timedelta(hours=1)
        )
    assert changed is True  # claim реально снят, helped_count пересчитан
    async with session_scope() as s:
        call = await supports.get_call(s, ctx.call_id)
        assert call.status == SupportCallStatus.closed  # НЕ воскрес


async def test_unclaim_reopens_call_closed_by_reaching_capacity(db) -> None:
    """closed ИМЕННО от заполнения нормы (filled>=needed, обычный claim()) —
    здесь reopen на unclaim корректен: слот реально освободился."""
    ctx = await _call_with_pool_candidate()  # needed=2
    async with session_scope() as s:
        await supports.set_status(s, ctx.call_id, SupportCallStatus.open)
        g = await groups.get_group(s, ctx.group_id)
        b, _ = await persons.add_to_group(s, g, 701, "u", "B")
        await persons.set_gender(s, b.id, Gender.male)
        await supports.claim(s, ctx.call_id, ctx.sup_pid)
        await supports.claim(s, ctx.call_id, b.id)  # filled=2/2 → claim() закрыл сам
        call = await supports.get_call(s, ctx.call_id)
        assert call.status == SupportCallStatus.closed
    async with session_scope() as s:
        changed = await supports.unclaim(
            s, ctx.call_id, ctx.sup_pid, now=LESSON - timedelta(hours=1)
        )
    assert changed is True
    async with session_scope() as s:
        call = await supports.get_call(s, ctx.call_id)
        assert call.status == SupportCallStatus.open  # слот освободился — открыт


async def test_support_join_records_claim(db) -> None:
    ctx = await _call_with_pool_candidate()
    async with session_scope() as s:
        await supports.set_status(s, ctx.call_id, SupportCallStatus.open)
    bot = FakeBot()
    await cb_support_join(FakeCallback(f"sup:join:{ctx.call_id}", user_id=700), bot)
    async with session_scope() as s:
        assert await supports.count_claims(s, ctx.call_id) == 1
    assert any(chat == 100 for chat, _ in bot.sent)  # teacher notified


async def test_support_join_unknown_person_gets_alert(db) -> None:
    """Тап от человека без Person-строки в этой школе — раньше тихий
    call.answer() без текста выглядел как сломанная кнопка."""
    ctx = await _call_with_pool_candidate()
    async with session_scope() as s:
        await supports.set_status(s, ctx.call_id, SupportCallStatus.open)
    bot = FakeBot()
    cb = FakeCallback(f"sup:join:{ctx.call_id}", user_id=999999)
    await cb_support_join(cb, bot)
    assert cb.answers and "нет в этой школе" in cb.answers[0]
    async with session_scope() as s:
        assert await supports.count_claims(s, ctx.call_id) == 0


async def test_support_skip_no_double_space_when_context_missing(db) -> None:
    """call/survey/group не находятся (битый/устаревший callback) — текст не
    должен получать двойной пробел из пустого `when` (title остаётся
    дефолтным «занятие»)."""
    bot = FakeBot()
    cb = FakeCallback("sup:skip:999999", user_id=700)
    await cb_support_skip(cb, bot)
    assert cb.message.text == "Понял, на «занятие» не сможешь. Спасибо, что откликнулся!"


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


# ── #5: claim после начала занятия ───────────────────────────────────


async def test_claim_after_lesson_rejected(db) -> None:
    """Поздний тап «приду выручить» после начала занятия не засчитывается."""
    past = datetime.now(UTC) - timedelta(hours=1)
    async with session_scope() as s:
        t = await admins.get_or_create(s, 100, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, past
        )
        call = await supports.create_call(
            s, survey.id, Gender.male, 2, SupportCallStatus.open
        )
        await persons.add_to_group(s, g, 700, "u", "Поздний")
        call_id = call.id
    bot = FakeBot()
    cb = FakeCallback(f"sup:join:{call_id}", user_id=700)
    await cb_support_join(cb, bot)
    async with session_scope() as s:
        assert await supports.count_claims(s, call_id) == 0
    assert cb.message.text and "уже началось" in cb.message.text.lower()


async def test_skip_after_lesson_rejected(db) -> None:
    """Поздний «не смогу» после начала занятия не стирает явку задним числом —
    зеркало гарда в cb_support_join (join-гард уже покрыт test_claim_after_lesson_rejected)."""
    async with session_scope() as s:
        t = await admins.get_or_create(s, 100, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, LESSON
        )
        call = await supports.create_call(
            s, survey.id, Gender.male, 2, SupportCallStatus.open
        )
        p, _ = await persons.add_to_group(s, g, 700, "u", "Саппорт")
        await supports.claim(s, call.id, p.id)
        call_id, survey_id, p_id = call.id, survey.id, p.id
    # занятие «прошло» — сдвигаем lesson_at в прошлое, как в test_claim_after_lesson_rejected
    async with session_scope() as s:
        survey = await surveys.get_survey(s, survey_id)
        survey.lesson_at = datetime.now(UTC) - timedelta(hours=1)
    bot = FakeBot()
    cb = FakeCallback(f"sup:skip:{call_id}", user_id=700)
    await cb_support_skip(cb, bot)
    async with session_scope() as s:
        assert await supports.count_claims(s, call_id) == 1  # claim жив
        claim_row = (await s.execute(
            select(supports.SupportClaim).where(
                supports.SupportClaim.support_call_id == call_id,
                supports.SupportClaim.person_id == p_id,
            )
        )).scalar_one()
        assert claim_row.attended is None  # не тронут
        assert (await s.get(Person, p_id)).helped_count == 0  # не тронут
    assert bot.sent == []  # лиду НЕ ушёл «передумал»-пуш
    assert cb.message.text and "прошло" in cb.message.text.lower()


# ── #3: таймер «тишины» считается от отправки инвайтов ───────────────


async def test_mark_invited_resets_silence_timer(db) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 100, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, LESSON
        )
        call = await supports.create_call(
            s, survey.id, Gender.male, 2, SupportCallStatus.pending
        )
        # call создан давно и его уже как-то пинговали
        call.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=5)
        call.no_reply_notified = True
        call_id = call.id
    async with session_scope() as s:
        await supports.mark_invited(s, call_id)
    async with session_scope() as s:
        call = await supports.get_call(s, call_id)
        assert call.status == SupportCallStatus.open
        assert call.no_reply_notified is False
        now_naive = datetime.now(UTC).replace(tzinfo=None)
        stale = await supports.stale_open_calls(s, now_naive, timedelta(hours=1))
        assert call_id not in [c.id for c in stale]  # таймер сброшен на «сейчас»


# ── #4: нудж по зависшему pending-call'у ─────────────────────────────


async def test_pending_call_nudges_owner_when_lesson_near(db) -> None:
    soon = datetime.now(UTC) + timedelta(hours=2)
    async with session_scope() as s:
        t = await admins.get_or_create(s, 100, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, soon
        )
        await supports.create_call(
            s, survey.id, Gender.male, 2, SupportCallStatus.pending
        )
    bot = FakeBot()
    await sched._notify_stale_support_calls(bot, datetime.now(UTC))
    assert any("не разобран" in txt for _, txt in bot.sent)
    # идемпотентно — повторно не пингуем
    await sched._notify_stale_support_calls(bot, datetime.now(UTC))
    assert sum(1 for _, txt in bot.sent if "не разобран" in txt) == 1


async def test_pending_call_quiet_when_lesson_far(db) -> None:
    """Занятие далеко (> PENDING_NUDGE_LEAD) — pending не дёргаем."""
    far = datetime.now(UTC) + timedelta(hours=30)
    async with session_scope() as s:
        t = await admins.get_or_create(s, 100, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, far
        )
        await supports.create_call(
            s, survey.id, Gender.male, 2, SupportCallStatus.pending
        )
    bot = FakeBot()
    await sched._notify_stale_support_calls(bot, datetime.now(UTC))
    assert bot.sent == []


async def test_stale_open_call_no_nudge_after_lesson_started(db) -> None:
    """Открытый call без откликов старше часа, но занятие УЖЕ началось — поздно
    советовать «напиши им лично», нужен реальный фильтр по lesson_at."""
    past = datetime.now(UTC) - timedelta(minutes=5)  # занятие уже началось
    async with session_scope() as s:
        t = await admins.get_or_create(s, 100, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, past
        )
        call = await supports.create_call(
            s, survey.id, Gender.male, 2, SupportCallStatus.open
        )
        call.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2)
        await s.flush()
    bot = FakeBot()
    await sched._notify_stale_support_calls(bot, datetime.now(UTC))
    assert bot.sent == []  # занятие уже началось — не советуем "напиши лично"


async def test_stale_open_call_not_marked_notified_on_send_failure(db) -> None:
    """Флаг no_reply_notified не должен ставиться, если реальная отправка
    упала — иначе единственный пинг теряется навсегда без повтора."""
    soon = datetime.now(UTC) + timedelta(hours=2)  # занятие ещё далеко
    async with session_scope() as s:
        t = await admins.get_or_create(s, 100, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, soon
        )
        call = await supports.create_call(
            s, survey.id, Gender.male, 2, SupportCallStatus.open
        )
        call.created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2)
        call_id = call.id
        await s.flush()

    class _BrokenBot:
        async def send_message(self, *args, **kwargs):
            raise RuntimeError("Telegram недоступен")

    await sched._notify_stale_support_calls(_BrokenBot(), datetime.now(UTC))
    async with session_scope() as s:
        call = await supports.get_call(s, call_id)
        assert call.no_reply_notified is False  # не потерян — попробуем ещё раз

    # На следующий тик с живым ботом пинг реально доходит.
    bot = FakeBot()
    await sched._notify_stale_support_calls(bot, datetime.now(UTC))
    assert any("не откликнулся" in txt for _, txt in bot.sent)
    async with session_scope() as s:
        call = await supports.get_call(s, call_id)
        assert call.no_reply_notified is True


# ── #6: helped_count по факту явки ───────────────────────────────────


async def test_support_attendance_drives_helped_count(db) -> None:
    from app.bot.handlers.attendance import cb_support_attendance_toggle
    from app.db.models import SupportClaim

    ctx = await _call_with_pool_candidate()
    async with session_scope() as s:
        await supports.set_status(s, ctx.call_id, SupportCallStatus.open)
    bot = FakeBot()
    await cb_support_join(FakeCallback(f"sup:join:{ctx.call_id}", user_id=700), bot)
    async with session_scope() as s:
        # отклик есть, но явка ещё не отмечена → 0
        assert (await s.get(Person, ctx.sup_pid)).helped_count == 0
        claim = (await s.execute(
            select(SupportClaim).where(SupportClaim.person_id == ctx.sup_pid)
        )).scalar_one()
        claim_id = claim.id
        survey_id = (await supports.get_call(s, ctx.call_id)).survey_id

    # препод отмечает «пришёл» в пост-leson ростере (код "x" = текущее None,
    # как и рисует keyboards._attended_code)
    await cb_support_attendance_toggle(
        FakeCallback(f"supatt:{survey_id}:{claim_id}:x:0", user_id=100)
    )
    async with session_scope() as s:
        assert (await s.get(Person, ctx.sup_pid)).helped_count == 1

    # переключение в «не пришёл» → helped_count снова 0
    await cb_support_attendance_toggle(
        FakeCallback(f"supatt:{survey_id}:{claim_id}:y:0", user_id=100)
    )
    async with session_scope() as s:
        assert (await s.get(Person, ctx.sup_pid)).helped_count == 0


# ── #7: ответственные за группу + маршрутизация ──────────────────────


async def test_group_admin_routing_and_access(db) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 100, "t", "Owner")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        gid, school_id, owner_id = g.id, g.school_id, t.id
        # group_admin_tg_ids (сводки/пинги): без назначения → все члены (owner)
        assert await groups.group_admin_tg_ids(s, gid, school_id) == [100]
        # recruit-доступ: lead не задан → fallback на члена школы
        assert await groups.can_manage_recruit(s, gid, school_id, owner_id) is True
        # назначаем lead'ом co-admin
        co = await admins.get_or_create(s, 555, "c", "Co")
        await schools.add_coadmin(s, school_id, co.id)
        await groups.set_group_admin(s, gid, co.id, True)
        assert set(await groups.group_admin_tg_ids(s, gid, school_id)) == {555}
        await groups.set_recruit_lead(s, gid, co.id)
        assert await groups.can_manage_recruit(s, gid, school_id, co.id) is True
        # owner больше НЕ lead → не может разобрать добор
        assert await groups.can_manage_recruit(s, gid, school_id, owner_id) is False


async def test_recruit_picker_goes_to_lead_only(db) -> None:
    g, _ = await _imbalanced_lesson(males_yes=1, females_yes=4)
    async with session_scope() as s:
        co = await admins.get_or_create(s, 555, "c", "Co")
        await schools.add_coadmin(s, g.school_id, co.id)
        await groups.set_recruit_lead(s, g.id, co.id)
        g = await groups.get_group(s, g.id)  # перечитать со свежим lead
    bot = FakeBot()
    await sched._maybe_recruit(bot, g, LESSON, LESSON - timedelta(hours=3))
    chats = {chat for chat, _ in bot.sent}
    assert 555 in chats  # picker ушёл lead'у (co-admin)
    assert 100 not in chats  # owner — нет (не lead)


async def test_create_group_assigns_lead_and_responsible(db) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 100, "t", "Owner")
        g = await groups.create_group(
            s, t.id, "G", "0", "19:00", 18, created_by_admin_id=t.id
        )
        assert g.recruit_lead_admin_id == t.id
        assert t.id in await groups.group_admin_ids(s, g.id)
        mine = await groups.my_groups(s, t.id)
        assert [x.id for x in mine] == [g.id]


async def test_pool_levels_are_per_teacher(db) -> None:
    """Один человек в пулах двух преподов с разными уровнями — независимо."""
    async with session_scope() as s:
        t1 = await admins.get_or_create(s, 100, "a", "A")
        t2 = await admins.get_or_create(s, 200, "b", "B")
        g = await groups.create_group(s, t1.id, "G", "0", "19:00", 18)
        await schools.add_coadmin(s, g.school_id, t2.id)
        p, _ = await persons.add_to_group(s, g, 700, "u", "Саппорт")
        await persons.set_pool_level(s, t1.id, p.id, 3)
        await persons.set_pool_level(s, t2.id, p.id, 1)
        assert (await persons.admin_pool_levels(s, t1.id)).get(p.id) == 3
        assert (await persons.admin_pool_levels(s, t2.id)).get(p.id) == 1
        # убрать из пула t1 не трогает пул t2
        await persons.set_pool_level(s, t1.id, p.id, None)
        assert (await persons.admin_pool_levels(s, t1.id)).get(p.id) is None
        assert (await persons.admin_pool_levels(s, t2.id)).get(p.id) == 1


async def test_pickmore_reveals_colleague_pool(db) -> None:
    """«➕ из пула коллег»: у lead'а свой пул пуст, но у коллеги есть саппорт."""
    async with session_scope() as s:
        t = await admins.get_or_create(s, 100, "t", "Owner")  # lead по fallback
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, LESSON
        )
        call = await supports.create_call(
            s, survey.id, Gender.male, 2, SupportCallStatus.pending
        )
        co = await admins.get_or_create(s, 555, "c", "Co")
        await schools.add_coadmin(s, g.school_id, co.id)
        other = await groups.create_group(s, g.school_id, "Сосед", "2", "20:00", 18)
        sup, _ = await persons.add_to_group(s, other, 701, "u", "Чужой пул")
        await persons.set_gender(s, sup.id, Gender.male)
        await persons.set_pool_level(s, co.id, sup.id, 2)  # в пуле коллеги, не lead'а
        call_id, sup_id = call.id, sup.id
    cb = FakeCallback(f"pickmore:{call_id}", user_id=100)
    await cb_support_pickmore(cb)
    callbacks = [
        b.callback_data
        for row in cb.message.reply_markup.inline_keyboard
        for b in row
    ]
    assert f"pick:{call_id}:{sup_id}" in callbacks  # саппорт коллеги показан


async def test_pickmore_excludes_already_claimed(db) -> None:
    """Уже согласившийся (claimed) не должен снова появляться невыбранным в
    picker'е после «➕ из пула коллег» — иначе выглядит как будто он ничего
    не подтверждал (фаза-2 фильтрует так же, см. scheduler.py claimed_ids)."""
    ctx = await _call_with_pool_candidate()  # needed=2, sup_pid в пуле lead'а
    async with session_scope() as s:
        await supports.set_status(s, ctx.call_id, SupportCallStatus.open)
        await supports.claim(s, ctx.call_id, ctx.sup_pid)  # уже согласился
        co = await admins.get_or_create(s, 555, "c", "Co")
        await schools.add_coadmin(s, ctx.school_id, co.id)
        other = await groups.create_group(s, ctx.school_id, "Сосед", "2", "20:00", 18)
        colleague_sup, _ = await persons.add_to_group(s, other, 702, "u", "Коллега")
        await persons.set_gender(s, colleague_sup.id, Gender.male)
        await persons.set_pool_level(s, co.id, colleague_sup.id, 2)
        colleague_id = colleague_sup.id
    cb = FakeCallback(f"pickmore:{ctx.call_id}", user_id=100)
    await cb_support_pickmore(cb)
    callbacks = [
        b.callback_data
        for row in cb.message.reply_markup.inline_keyboard
        for b in row
    ]
    assert f"pick:{ctx.call_id}:{ctx.sup_pid}" not in callbacks  # уже согласился
    assert f"pick:{ctx.call_id}:{colleague_id}" in callbacks  # ещё не спрошен


async def test_support_invite_known_person_added_to_pool(db) -> None:
    """Существующий человек открыл support-ссылку препода → попал в его пул."""
    from app.bot.handlers.student import _support_invite_start
    from tests._fakes import FakeBot as FB
    from tests._fakes import FakeFSM, FakeMessage, fake_user

    async with session_scope() as s:
        owner = await admins.get_or_create(s, 100, "t", "Owner")
        g = await groups.create_group(s, owner.id, "G", "0", "19:00", 18)
        p, _ = await persons.add_to_group(s, g, 700, "u", "Знакомый")
        await persons.set_gender(s, p.id, Gender.male)
        code = await admins.get_or_create_support_code(s, owner.id)
        owner_id, pid = owner.id, p.id
    bot = FB()
    msg = FakeMessage(fake_user(700, full_name="Знакомый"), bot=bot)
    await _support_invite_start(msg, code, FakeFSM(), bot)
    async with session_scope() as s:
        assert (await persons.admin_pool_levels(s, owner_id)).get(pid) == 1


async def test_recruit_threshold_read_from_lead(db) -> None:
    """Настройки автодобора — личные на Admin(lead): высокий порог → добора нет."""
    g, survey_id = await _imbalanced_lesson(males_yes=1, females_yes=4)  # дефицит 3
    async with session_scope() as s:
        owner = await admins.get_by_tg_id(s, 100)  # lead по fallback
        await admins.set_support_threshold(s, owner.id, 5)  # 3 < 5
    bot = FakeBot()
    await sched._maybe_recruit(bot, g, LESSON, LESSON - timedelta(hours=3))
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, survey_id)
        assert call is not None and call.status == SupportCallStatus.closed
    assert bot.sent == []


async def test_picksend_blocked_when_already_handled(db) -> None:
    """Второй ответственный со «стейл» picker'ом не задваивает инвайты."""
    ctx = await _call_with_pool_candidate()
    async with session_scope() as s:
        await supports.set_status(s, ctx.call_id, SupportCallStatus.open)  # уже разобрано
        cands = await persons.support_candidates(
            s, ctx.admin_id, Gender.male, ctx.group_id
        )
    kb = support_pick_keyboard(ctx.call_id, cands, {ctx.sup_pid})
    bot = FakeBot()
    send = FakeCallback(f"picksend:{ctx.call_id}", user_id=100, reply_markup=kb)
    await cb_support_pick_send(send, bot)
    assert bot.sent == []
    assert send.answers and "разобрано" in send.answers[0].lower()


async def test_support_join_notifies_assigned_admin_not_owner(db) -> None:
    """Уведомление об отклике саппорта идёт ответственным за группу, не owner'у."""
    ctx = await _call_with_pool_candidate()
    async with session_scope() as s:
        co = await admins.get_or_create(s, 555, "c", "Co")
        await schools.add_coadmin(s, ctx.school_id, co.id)
        await groups.set_group_admin(s, ctx.group_id, co.id, True)
        await supports.set_status(s, ctx.call_id, SupportCallStatus.open)
    bot = FakeBot()
    await cb_support_join(FakeCallback(f"sup:join:{ctx.call_id}", user_id=700), bot)
    chats = {chat for chat, _ in bot.sent}
    assert 555 in chats  # ответственный co-admin уведомлён
    assert 100 not in chats  # owner — нет (не ответственный)
