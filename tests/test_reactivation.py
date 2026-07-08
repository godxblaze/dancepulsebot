"""Phase 6 (B5): реактивация молчунов — lapsed-запрос, throttle, негативные."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.db.models import Answer, SurveyKind, SurveyStatus
from app.db.session import session_scope
from app.scheduler import scheduler as sched
from app.services import admins as admins_svc
from app.services import groups, persons, surveys

_MSK = ZoneInfo("Europe/Moscow")
LESSON = (
    (datetime.now(UTC).astimezone(_MSK) + timedelta(days=14))
    .replace(hour=19, minute=0, second=0, microsecond=0)
    .astimezone(UTC)
)
PAST1 = LESSON - timedelta(days=7)   # самый свежий прошлый опрос
PAST2 = LESSON - timedelta(days=14)  # более старый
_NOW = LESSON - timedelta(hours=10)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs) -> None:
        self.sent.append((chat_id, text))


async def _setup_group():
    """lapsed(200): «нет» в обоих прошлых; active(201): «да» в обоих;
    newbie(202): без истории (только в ближайшем опросе)."""
    async with session_scope() as s:
        t = await admins_svc.get_or_create(s, 100, "t", "Препод")
        g = await groups.create_group(s, t.id, "Бачата", "0", "19:00", 18)
        lapsed, _ = await persons.add_to_group(s, g, 200, "u", "Лапсед")
        active, _ = await persons.add_to_group(s, g, 201, "u", "Активный")
        newbie, _ = await persons.add_to_group(s, g, 202, "u", "Новичок")
        for when in (PAST2, PAST1):
            sv, _ = await surveys.get_or_create_survey(
                s, g.id, SurveyKind.attendance, when
            )
            sv.status = SurveyStatus.closed
            await surveys.ensure_responses(s, sv.id, [lapsed.id, active.id])
            await surveys.record_answer(s, sv.id, lapsed.id, Answer.no)
            await surveys.record_answer(s, sv.id, active.id, Answer.yes)
        up, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, LESSON
        )
        await surveys.ensure_responses(s, up.id, [lapsed.id, active.id, newbie.id])
        await s.flush()
        return SimpleNamespace(
            group_id=g.id, lapsed=lapsed.id, active=active.id, newbie=newbie.id,
            lapsed_tg=200, active_tg=201, newbie_tg=202,
        )


async def _get_group(gid):
    async with session_scope() as s:
        return await groups.get_group(s, gid)


async def test_lapsed_members_query_identifies_only_lapsed(db) -> None:
    ids = await _setup_group()
    async with session_scope() as s:
        lapsed = await surveys.lapsed_members(s, ids.group_id, k=2)
    assert [p.id for p in lapsed] == [ids.lapsed]


async def test_lapsed_members_ignores_cancelled_lessons(db) -> None:
    """Отменённые занятия тоже закрывают свой Survey (lesson_cancel.py). Без
    анти-джойна против LessonCancellation они бы заняли место в «последних k»
    и активный член (реально пришедший на оба genuine занятия) ложно
    определился бы как lapsed — потому что в момент отмены survey уже был
    отвечен «нет» (человек знал что не идёт ДО отмены, а потом занятие всё
    равно отменили)."""
    from app.services import cancellations

    async with session_scope() as s:
        t = await admins_svc.get_or_create(s, 100, "t", "Препод")
        g = await groups.create_group(s, t.id, "Бачата", "0", "19:00", 18)
        active, _ = await persons.add_to_group(s, g, 201, "u", "Активный")
        # 2 genuine прошлых занятия — активный реально приходил (yes) на оба.
        for when in (PAST2, PAST1):
            sv, _ = await surveys.get_or_create_survey(s, g.id, SurveyKind.attendance, when)
            sv.status = SurveyStatus.closed
            await surveys.ensure_responses(s, sv.id, [active.id])
            await surveys.record_answer(s, sv.id, active.id, Answer.yes)
        # 2 отменённых занятия БЛИЖЕ к сегодня, чем genuine — если их не
        # отфильтровать, они займут окно «последних k=2» вместо genuine.
        for days_ago in (2, 4):
            when = LESSON - timedelta(days=days_ago)
            sv, _ = await surveys.get_or_create_survey(s, g.id, SurveyKind.attendance, when)
            await surveys.ensure_responses(s, sv.id, [active.id])
            await surveys.record_answer(s, sv.id, active.id, Answer.no)
            sv.status = SurveyStatus.closed
            await cancellations.cancel(s, g.id, when)
        gid, active_id = g.id, active.id
    async with session_scope() as s:
        lapsed = await surveys.lapsed_members(s, gid, k=2)
    # Без фильтра последние k=2 были бы оба отменённых («нет» на обоих) →
    # ложный lapsed. С фильтром последние k=2 — genuine PAST1/PAST2 (оба yes).
    assert active_id not in [p.id for p in lapsed]


async def test_lapsed_members_ignores_backfilled_empty_survey(db) -> None:
    """Survey без единой Response-строки — бэкфилл занятия, пропущенного во
    время даунтайма бота (см. scheduler.backfill_missed_surveys). Без фильтра
    он занял бы место в «последних k», и т.к. ни у кого нет там Response,
    lapsed_members молча вернул бы пусто вместо реального лапседа."""
    ids = await _setup_group()
    async with session_scope() as s:
        backfilled, _ = await surveys.get_or_create_survey(
            s, ids.group_id, SurveyKind.attendance, PAST1 - timedelta(days=1)
        )
        backfilled.status = SurveyStatus.closed
        backfilled.summary_sent = True
        await s.flush()
    async with session_scope() as s:
        lapsed = await surveys.lapsed_members(s, ids.group_id, k=2)
    assert [p.id for p in lapsed] == [ids.lapsed]


async def test_lapsed_member_gets_one_reactivation(db) -> None:
    ids = await _setup_group()
    group = await _get_group(ids.group_id)
    bot = FakeBot()
    await sched._maybe_reactivate(bot, group, LESSON, _NOW)
    pinged = [c for c, _ in bot.sent]
    assert pinged == [ids.lapsed_tg]  # ровно один пинг, только lapsed
    assert any("соскучились" in t.lower() for _, t in bot.sent)


async def test_reactivation_throttled_on_repeat(db) -> None:
    ids = await _setup_group()
    group = await _get_group(ids.group_id)
    bot = FakeBot()
    await sched._maybe_reactivate(bot, group, LESSON, _NOW)
    assert len(bot.sent) == 1
    # повторный запуск в окне throttle — второй раз не шлём
    await sched._maybe_reactivate(bot, group, LESSON, _NOW)
    assert len(bot.sent) == 1


async def test_reactivation_fires_again_after_throttle_window(db) -> None:
    ids = await _setup_group()
    group = await _get_group(ids.group_id)
    bot = FakeBot()
    await sched._maybe_reactivate(bot, group, LESSON, _NOW)
    assert len(bot.sent) == 1
    # запуск через >14 дней (по «часам» планировщика) — снова можно
    later = _NOW + timedelta(days=15)
    await sched._maybe_reactivate(bot, group, LESSON, later)
    assert len(bot.sent) == 2


async def test_active_member_not_reactivated(db) -> None:
    ids = await _setup_group()
    group = await _get_group(ids.group_id)
    bot = FakeBot()
    await sched._maybe_reactivate(bot, group, LESSON, _NOW)
    assert ids.active_tg not in {c for c, _ in bot.sent}


async def test_new_member_without_history_not_reactivated(db) -> None:
    ids = await _setup_group()
    group = await _get_group(ids.group_id)
    bot = FakeBot()
    await sched._maybe_reactivate(bot, group, LESSON, _NOW)
    assert ids.newbie_tg not in {c for c, _ in bot.sent}
