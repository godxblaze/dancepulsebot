"""Phase 8: edge-кейсы автодобора — отменённое занятие, пустой пул всех режимов,
смена стороны дефицита, идемпотентность повторных tick'ов."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.db.models import Answer, Gender, SupportCallStatus, SurveyKind
from app.db.session import session_scope
from app.scheduler import scheduler as sched
from app.services import admins as admins_svc
from app.services import cancellations, groups, persons, supports, surveys
from tests._outbox_helpers import drain_outbox

_MSK = ZoneInfo("Europe/Moscow")


def _msk(y, mo, d, h, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=_MSK).astimezone(UTC)


MONDAY_LESSON = _msk(2026, 5, 25, 19, 0)  # 2026-05-25 — понедельник
LESSON_F = (
    (datetime.now(UTC).astimezone(_MSK) + timedelta(days=14))
    .replace(hour=19, minute=0, second=0, microsecond=0)
    .astimezone(UTC)
)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.edits: list[tuple] = []
        self._mid = 1000

    async def send_message(self, chat_id, text, **kwargs):
        self._mid += 1
        self.sent.append((chat_id, text))
        return SimpleNamespace(chat_id=chat_id, message_id=self._mid, text=text)

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        self.edits.append((chat_id, message_id, text))


@pytest.fixture
def freeze(monkeypatch):
    def _set(dt: datetime) -> None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_MSK).astimezone(UTC)
        monkeypatch.setattr(sched, "utc_now", lambda: dt)

    return _set


async def _tick(bot) -> None:
    await sched.tick(bot)
    await drain_outbox(bot)


async def _recruit(bot, group, lesson_at, now) -> None:
    await sched._maybe_recruit(bot, group, lesson_at, now)
    await drain_outbox(bot)


async def _imbalance(mode: str, *, with_pool: bool = False):
    """Дефицит 3 парней; lead(100) с режимом mode; опционально 2 парня в пуле."""
    async with session_scope() as s:
        t = await admins_svc.get_or_create(s, 100, "t", "Препод")
        await admins_svc.set_recruit_mode(s, t.id, mode)
        g = await groups.create_group(s, t.id, "Бачата", "0", "19:00", 18)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, LESSON_F
        )
        uid = 200
        for gen, count in ((Gender.male, 1), (Gender.female, 4)):
            for _ in range(count):
                p, _ = await persons.add_to_group(s, g, uid, "u", f"P{uid}")
                await persons.set_gender(s, p.id, gen)
                await surveys.record_answer(s, survey.id, p.id, Answer.yes)
                uid += 1
        pool_tgs: list[int] = []
        if with_pool:
            other = await groups.create_group(s, t.id, "Пул", "2", "20:00", 18)
            for lvl, tg in ((2, 301), (1, 302)):
                p, _ = await persons.add_to_group(s, other, tg, "u", f"Пул{lvl}")
                await persons.set_gender(s, p.id, Gender.male)
                await persons.set_pool_level(s, t.id, p.id, lvl)
                pool_tgs.append(tg)
        return SimpleNamespace(group=g, survey_id=survey.id, pool_tgs=pool_tgs)


# ── отменённое занятие ───────────────────────────────────────────────


async def test_cancelled_lesson_no_poll_recruit_or_reactivation(db, freeze) -> None:
    async with session_scope() as s:
        t = await admins_svc.get_or_create(s, 111, "t", "Препод")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        await persons.add_to_group(s, g, 200, "u", "Ученик")
        await cancellations.cancel(s, g.id, MONDAY_LESSON)
        gid = g.id
    freeze(datetime(2026, 5, 25, 9, 0))  # в окне, ~10ч до занятия
    bot = FakeBot()
    await _tick(bot)
    assert bot.sent == []  # ни опроса, ни добора, ни реактивации
    async with session_scope() as s:
        assert await surveys.find_attendance_survey(s, gid, MONDAY_LESSON) is None


# ── пустой пул во всех режимах ───────────────────────────────────────


async def test_curated_empty_pool_graceful(db) -> None:
    ctx = await _imbalance("curated")  # пул пуст
    bot = FakeBot()
    await _recruit(bot, ctx.group, LESSON_F, LESSON_F - timedelta(hours=3))
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, ctx.survey_id)
        assert call is not None and call.status == SupportCallStatus.pending
    assert any("нет подходящих" in t.lower() for c, t in bot.sent if c == 100)


async def test_escalate_empty_pool_graceful(db) -> None:
    ctx = await _imbalance("escalate")
    # Фаза 1 — pending picker до окна эскалации
    await _recruit(
        FakeBot(), ctx.group, LESSON_F, LESSON_F - timedelta(hours=20)
    )
    bot = FakeBot()
    # окно эскалации, пустой пул → call open, lead «нет подходящих», без падения
    await _recruit(bot, ctx.group, LESSON_F, LESSON_F - timedelta(hours=4))
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, ctx.survey_id)
        assert call is not None and call.status == SupportCallStatus.open
    assert any("нет подходящих" in t.lower() for c, t in bot.sent if c == 100)


# ── идемпотентность повторных tick'ов ────────────────────────────────


async def test_auto_top_idempotent_no_double_invite(db) -> None:
    ctx = await _imbalance("auto_top", with_pool=True)
    bot = FakeBot()
    await _recruit(bot, ctx.group, LESSON_F, LESSON_F - timedelta(hours=3))
    first = sum(1 for _, t in bot.sent if "Нужна помощь" in t)
    assert first == len(ctx.pool_tgs) and first > 0
    # повторный tick: call уже open → новых приглашений нет
    bot2 = FakeBot()
    await _recruit(bot2, ctx.group, LESSON_F, LESSON_F - timedelta(hours=3))
    assert not any("Нужна помощь" in t for _, t in bot2.sent)


# ── смена стороны дефицита (top-up не флипает) ───────────────────────


async def test_topup_does_not_flip_when_deficit_changes_side(db) -> None:
    ctx = await _imbalance("curated")  # 1 парень / 4 девушки → нужно 3 парней
    await _recruit(
        FakeBot(), ctx.group, LESSON_F, LESSON_F - timedelta(hours=20)
    )
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, ctx.survey_id)
        assert call.gender == Gender.male
        # приходит много парней → перекос уходит в сторону девушек
        g = await groups.get_group(s, ctx.group.id)
        uid = 900
        for _ in range(5):
            p, _ = await persons.add_to_group(s, g, uid, "u", f"M{uid}")
            await persons.set_gender(s, p.id, Gender.male)
            await surveys.record_answer(s, ctx.survey_id, p.id, Answer.yes)
            uid += 1
    bot = FakeBot()
    await _recruit(bot, ctx.group, LESSON_F, LESSON_F - timedelta(hours=2))
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, ctx.survey_id)
        assert call.gender == Gender.male  # не флипнули на девушек
        assert call.topup_done is True  # top-up отработал (без новой рассылки)
    assert bot.sent == []  # ничего нового не звали
