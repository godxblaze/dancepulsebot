"""Tests for the durable send queue: enqueue idempotency, concurrent dequeue
safety, rate limiting, TelegramRetryAfter handling, terminal failure, and the
sender worker's start/stop lifecycle. Nothing here touches real send-loops
(scheduler.py/misc_dialogs.py) — that's Phase 4/5."""
import asyncio
import time
from datetime import timedelta
from types import SimpleNamespace

from aiogram.exceptions import TelegramRetryAfter
from sqlalchemy import select

from app.bot.outbox_worker import SUMMARY_MAX_ATTEMPTS, TokenBucket, _process_one, run_sender_worker
from app.db.models import MessageOutbox
from app.db.session import session_scope
from app.services import outbox as outbox_svc
from app.services.schedule import utc_now


class _ScriptedBot:
    """Fake Bot for outbox tests — send_message's outcome per chat_id is
    scripted (success / raise), consumed in order."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self._script: dict[int, list[object]] = {}

    def script_for(self, chat_id: int, behaviors: list) -> None:
        self._script[chat_id] = list(behaviors)

    async def send_message(self, chat_id: int, text: str, **_: object) -> SimpleNamespace:
        behaviors = self._script.get(chat_id)
        if behaviors:
            behavior = behaviors.pop(0)
            if isinstance(behavior, Exception):
                raise behavior
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.sent))


async def _row_by_key(business_key: str) -> MessageOutbox:
    async with session_scope() as s:
        return (
            await s.execute(select(MessageOutbox).where(MessageOutbox.business_key == business_key))
        ).scalar_one()


async def test_enqueue_idempotent_on_business_key(db) -> None:
    async with session_scope() as s:
        await outbox_svc.enqueue(s, "k1", 1, {"text": "a"})
        await outbox_svc.enqueue(s, "k1", 1, {"text": "b"})  # тот же ключ — no-op
    async with session_scope() as s:
        rows = (await s.execute(select(MessageOutbox))).scalars().all()
    assert len(rows) == 1
    assert rows[0].payload_json["text"] == "a"


async def test_concurrent_dequeue_no_duplicate_delivery(db) -> None:
    n = 20
    async with session_scope() as s:
        for i in range(n):
            await outbox_svc.enqueue(s, f"k{i}", i, {"text": "x"})

    claimed_ids: list[int] = []
    lock = asyncio.Lock()

    async def claim_one() -> None:
        async with session_scope() as s:
            row = await outbox_svc.claim_next(s)
            if row is None:
                return
            async with lock:
                claimed_ids.append(row.id)
            await outbox_svc.mark_sent(s, row, None)

    # n concurrent claimers racing the same pool of n pending rows — each
    # opens its own DB connection/transaction, so this genuinely exercises
    # `FOR UPDATE SKIP LOCKED` racing, not just cooperative interleaving.
    await asyncio.gather(*(claim_one() for _ in range(n)))

    assert len(claimed_ids) == n
    assert len(set(claimed_ids)) == n  # zero duplicate delivery


async def test_rate_limiter_caps_throughput() -> None:
    limiter = TokenBucket(rate=20, capacity=2)
    start = time.monotonic()
    for _ in range(12):  # 2 burst tokens + 10 metered at 20/s ≈ 0.5s minimum
        await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.4  # not near-instant — the limiter is actually throttling
    assert elapsed < 3.0  # sanity ceiling — not stuck/hung


async def test_retry_after_reschedules_and_does_not_block_others(db) -> None:
    bot = _ScriptedBot()
    bot.script_for(1, [TelegramRetryAfter(method=None, message="flood", retry_after=999)])
    async with session_scope() as s:
        await outbox_svc.enqueue(s, "k-retry", 1, {"text": "x"})
        await outbox_svc.enqueue(s, "k-other", 2, {"text": "y"})

    limiter = TokenBucket(rate=1000)
    assert await _process_one(bot, limiter) is True
    assert await _process_one(bot, limiter) is True

    assert bot.sent == [(2, "y")]  # только неблокированная строка реально ушла

    retry_row = await _row_by_key("k-retry")
    other_row = await _row_by_key("k-other")
    assert retry_row.status == "pending"
    assert retry_row.scheduled_at > utc_now() + timedelta(seconds=900)
    assert other_row.status == "sent"


async def test_terminal_failure_after_max_attempts(db) -> None:
    bot = _ScriptedBot()
    bot.script_for(1, [RuntimeError("boom")] * outbox_svc.MAX_ATTEMPTS)
    async with session_scope() as s:
        await outbox_svc.enqueue(s, "k-fail", 1, {"text": "x"})

    limiter = TokenBucket(rate=1000)
    for _ in range(outbox_svc.MAX_ATTEMPTS):
        # Backoff pushes scheduled_at into the future between attempts — force
        # it back to "now" so the test doesn't need to sleep real wall-clock.
        async with session_scope() as s:
            row = (
                await s.execute(select(MessageOutbox).where(MessageOutbox.business_key == "k-fail"))
            ).scalar_one()
            row.scheduled_at = utc_now()
        assert await _process_one(bot, limiter) is True

    row = await _row_by_key("k-fail")
    assert row.status == "terminal_failed"
    assert row.attempts == outbox_svc.MAX_ATTEMPTS
    assert row.last_error and "boom" in row.last_error

    async with session_scope() as s:
        assert await outbox_svc.claim_next(s) is None  # больше не забирается


async def test_summary_kind_survives_past_generic_max_attempts(db) -> None:
    """Phase 12 fix: auto-summary sends get SUMMARY_MAX_ATTEMPTS (200), not
    the generic MAX_ATTEMPTS (5) — a blocked/not-yet-started admin shouldn't
    lose their lesson summary forever after ~1 minute of exponential backoff
    (the pre-outbox behavior retried every 15-min tick for the survey's whole
    open lifetime, often many hours)."""
    bot = _ScriptedBot()
    n = outbox_svc.MAX_ATTEMPTS + 2
    bot.script_for(1, [RuntimeError("boom")] * n)
    async with session_scope() as s:
        await outbox_svc.enqueue(s, "k-summary", 1, {"text": "x", "kind": "summary"})

    limiter = TokenBucket(rate=1000)
    for _ in range(n):
        async with session_scope() as s:
            row = (
                await s.execute(
                    select(MessageOutbox).where(MessageOutbox.business_key == "k-summary")
                )
            ).scalar_one()
            row.scheduled_at = utc_now()
        assert await _process_one(bot, limiter) is True

    row = await _row_by_key("k-summary")
    # Прошли generic MAX_ATTEMPTS (5), но всё ещё pending — не потеряна.
    assert row.status == "pending"
    assert row.attempts == n
    assert n < SUMMARY_MAX_ATTEMPTS


async def test_sender_worker_starts_and_stops_cleanly(db) -> None:
    bot = _ScriptedBot()
    stop_event = asyncio.Event()
    task = asyncio.create_task(run_sender_worker(bot, stop_event))
    await asyncio.sleep(0.05)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)  # завершается сразу, не висит до POLL_INTERVAL
    assert task.done() and not task.cancelled()
    assert task.exception() is None
