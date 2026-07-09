"""Phase 9: unit tests for the new devstats.py metrics — outbox health/send
counts (windowed, same convention as school_row), and the thin passthroughs
to the in-process live-gauges (db session duration, callback latency,
support-claim conflicts)."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.bot.middlewares import CallbackLatencyMiddleware
from app.db.models import Gender, MessageOutbox, SupportCallStatus, SurveyKind
from app.db.session import session_scope
from app.services import admins, groups, persons, supports, surveys
from app.services import devstats as devstats_svc
from app.services import outbox as outbox_svc

_MSK = ZoneInfo("Europe/Moscow")
LESSON = (
    (datetime.now(UTC).astimezone(_MSK) + timedelta(days=14))
    .replace(hour=19, minute=0, second=0, microsecond=0)
    .astimezone(UTC)
)


async def test_outbox_health_reports_pending_count_and_oldest_age(db) -> None:
    async with session_scope() as s:
        await outbox_svc.enqueue(s, "k1", 1, {"text": "a"})
        await outbox_svc.enqueue(s, "k2", 2, {"text": "b"})
    async with session_scope() as s:
        old_row = (await s.execute(
            MessageOutbox.__table__.select().where(MessageOutbox.business_key == "k1")
        )).first()
        row = await s.get(MessageOutbox, old_row.id)
        row.scheduled_at = datetime.now(UTC) - timedelta(minutes=45)

    async with session_scope() as s:
        health = await devstats_svc.outbox_health(s)
    assert health["pending"] == 2
    # -5с запаса на дрожание времени между записью и проверкой в тесте.
    assert health["oldest_pending_age_seconds"] >= 45 * 60 - 5


async def test_outbox_health_empty_queue(db) -> None:
    async with session_scope() as s:
        health = await devstats_svc.outbox_health(s)
    assert health == {"pending": 0, "oldest_pending_age_seconds": None}


async def test_outbox_send_counts_windowed(db) -> None:
    since = datetime.now(UTC) - timedelta(days=1)
    async with session_scope() as s:
        await outbox_svc.enqueue(s, "sent-1", 1, {"text": "a"})
        await outbox_svc.enqueue(s, "failed-1", 2, {"text": "b"})
        await outbox_svc.enqueue(s, "blocked-1", 3, {"text": "c"})
        await outbox_svc.enqueue(s, "retrying-1", 4, {"text": "d"})
    async with session_scope() as s:
        rows = {
            r.business_key: r
            for r in (await s.execute(MessageOutbox.__table__.select())).all()
        }
        sent_row = await s.get(MessageOutbox, rows["sent-1"].id)
        sent_row.status = "sent"
        sent_row.sent_at = datetime.now(UTC)

        failed_row = await s.get(MessageOutbox, rows["failed-1"].id)
        failed_row.status = "terminal_failed"
        failed_row.last_error = "RuntimeError: boom"

        blocked_row = await s.get(MessageOutbox, rows["blocked-1"].id)
        blocked_row.status = "terminal_failed"
        blocked_row.last_error = "Telegram server says - Forbidden: bot was blocked by the user"

        retrying_row = await s.get(MessageOutbox, rows["retrying-1"].id)
        retrying_row.attempts = 2

    async with session_scope() as s:
        counts = await devstats_svc.outbox_send_counts(s, since)
    assert counts == {"sent": 1, "failed": 2, "blocked": 1, "retrying": 1}


async def test_session_duration_stats_accumulates(db) -> None:
    before = devstats_svc.session_duration_stats()
    async with session_scope():
        pass
    after = devstats_svc.session_duration_stats()
    assert after["count"] > before["count"]
    assert after["avg_seconds"] >= 0.0


async def test_callback_latency_stats_accumulates() -> None:
    before = devstats_svc.callback_latency_stats()

    async def _handler(event, data) -> str:
        return "ok"

    mw = CallbackLatencyMiddleware()
    result = await mw(_handler, SimpleNamespace(), {})
    assert result == "ok"

    after = devstats_svc.callback_latency_stats()
    assert after["count"] == before["count"] + 1


async def test_support_claim_conflict_count_increments_on_real_race(db) -> None:
    """Заполненный call → третий, конкурирующий claim другим человеком —
    настоящая гонка (не повторный тап тем же человеком) должна инкрементить
    счётчик. Дублирующий тап тем же человеком — не должен."""
    async with session_scope() as s:
        t = await admins.get_or_create(s, 900, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        survey, _ = await surveys.get_or_create_survey(s, g.id, SurveyKind.attendance, LESSON)
        # needed=2: call остаётся open после первого claim — повторный тап
        # тем же человеком попадает в "dup"-ветку, не в "closed".
        dup_call = await supports.create_call(s, survey.id, Gender.male, 2, SupportCallStatus.open)
        # needed=1: второй (другим человеком) claim попадает в "закрыт" — гонка.
        race_survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.feedback, LESSON
        )
        race_call = await supports.create_call(
            s, race_survey.id, Gender.male, 1, SupportCallStatus.open
        )
        p1, _ = await persons.add_to_group(s, g, 901, "u1", "P1")
        p2, _ = await persons.add_to_group(s, g, 902, "u2", "P2")
        dup_call_id, race_call_id, p1_id, p2_id = dup_call.id, race_call.id, p1.id, p2.id

    before = devstats_svc.support_claim_conflict_count()
    async with session_scope() as s:
        accepted, *_ = await supports.claim(s, dup_call_id, p1_id)
    assert accepted is True
    async with session_scope() as s:
        accepted_dup, *_ = await supports.claim(s, dup_call_id, p1_id)  # тот же человек — не гонка
    assert accepted_dup is False
    assert devstats_svc.support_claim_conflict_count() == before  # не инкрементилось

    async with session_scope() as s:
        accepted_p1, *_ = await supports.claim(s, race_call_id, p1_id)
    assert accepted_p1 is True  # заполнил единственный слот, call закрылся
    async with session_scope() as s:
        accepted_race, *_ = await supports.claim(s, race_call_id, p2_id)  # слот занят другим
    assert accepted_race is False
    assert devstats_svc.support_claim_conflict_count() == before + 1
