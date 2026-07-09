"""Sender worker: claims `message_outbox` rows one at a time and delivers
them through a single global rate limiter. Runs as its own asyncio task,
independent of the scheduler's heartbeat/tick — started/stopped alongside
polling in `app/main.py`'s `_run_locked()`.

`_on_resolved` carries the per-message-type bookkeeping (mark delivered/
undeliverable, remember a picker's message ref, ...) that used to happen
inline in the old direct-send loops (scheduler.py/lesson_cancel.py/
recruit.py) — now driven off the outbox's own sent/terminal_failed outcomes
instead of a synchronous try/except around `bot.send_message`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup

from app.db.session import session_scope
from app.services import outbox as outbox_svc
from app.services import persons as persons_svc
from app.services import supports as supports_svc
from app.services import surveys as surveys_svc
from app.services.schedule import utc_now

log = logging.getLogger(__name__)

RATE_PER_SECOND = 25.0  # Telegram's documented soft global limit
POLL_INTERVAL = 1.0  # idle wait between empty-queue checks, seconds

# Phase 12 fix (load-test-replay/final-review found this): the generic
# outbox_svc.MAX_ATTEMPTS=5 exhausts in ~1 minute of backoff (2s+4s+8s+16s+32s)
# — far short of the pre-outbox behavior's actual intent for summaries, which
# was "keep retrying every 15-min tick for as long as the survey stays open"
# (often many hours — lead_hours can be 18-24). Since enqueue()'s business_key
# (`summary:{survey_id}:{tg}`) is fixed and ON CONFLICT DO NOTHING, a row that
# reaches terminal_failed can NEVER be revived by a later tick — the summary
# is silently lost forever. `_backoff()` caps at 300s past the initial
# exponential ramp (~510s for attempts 1-8), so 200 attempts ≈ 510s + 192×300s
# ≈ 16h — comfortably covers a lesson's full open-survey lifetime while still
# being a finite budget (an admin who blocks the bot forever doesn't retry
# forever, just for ~16h instead of ~1min).
SUMMARY_MAX_ATTEMPTS = 200


class TokenBucket:
    """asyncio-native token bucket — the single choke point every outbound
    send passes through. No per-call-site sleeping anywhere else."""

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        self.rate = rate
        self.capacity = capacity if capacity is not None else max(1.0, rate)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self.rate)


def _load_markup(data: dict | None) -> InlineKeyboardMarkup | None:
    return InlineKeyboardMarkup.model_validate(data) if data else None


async def _on_resolved(
    session, payload: dict, outcome: str, telegram_message_id: int | None, chat_id: int
) -> None:
    """`outcome` is `"sent"` or `"terminal_failed"` — transient reschedules
    never reach here. `payload["kind"]` picks the bookkeeping; kinds with no
    entry here (support_invite, lesson_cancel, ...) need none — delivery
    alone was the point."""
    kind = payload.get("kind")
    if kind == "attendance_poll":
        survey_id, person_id = payload["survey_id"], payload["person_id"]
        if outcome == "sent":
            await surveys_svc.set_message_id(session, survey_id, person_id, telegram_message_id)
            # Decided at enqueue time (scheduler.py), not recomputed here —
            # see the comment in _poll_group for why.
            if payload.get("mark_reminded_4h"):
                await surveys_svc.mark_reminded(session, survey_id, person_id, "4h")
            if payload.get("mark_reminded_1h"):
                await surveys_svc.mark_reminded(session, survey_id, person_id, "1h")
        else:
            await surveys_svc.set_message_id(session, survey_id, person_id, -1)
    elif kind == "reminder":
        # Both outcomes mark it — a permanently-undeliverable recipient
        # shouldn't be retried every tick either (mirrors the old
        # Forbidden/NotFound "give up quietly" path).
        await surveys_svc.mark_reminded(
            session, payload["survey_id"], payload["person_id"], payload["window"]
        )
    elif kind == "reactivation":
        # reactivated_at — вычислен при enqueue (см. _maybe_reactivate), не
        # повторным utc_now() здесь.
        await persons_svc.mark_reactivated(
            session, payload["person_id"], datetime.fromisoformat(payload["reactivated_at"])
        )
    elif kind == "summary":
        # Idempotent — fine to call once per admin recipient that succeeds.
        if outcome == "sent":
            await surveys_svc.mark_summary_sent(session, payload["survey_id"])
    elif kind == "recruit_picker":
        if outcome == "sent":
            await supports_svc.set_picker_ref(
                session, payload["call_id"], chat_id, telegram_message_id,
                has_keyboard=payload.get("has_keyboard", False),
            )
    elif kind == "recruit_lead_heads_up":
        if outcome == "sent":
            await supports_svc.set_picker_ref(
                session, payload["call_id"], chat_id, telegram_message_id
            )
    elif kind == "stale_notice":
        # Only on confirmed delivery — an undelivered notice should be
        # retried via the next tick's natural re-query, not marked done.
        if outcome == "sent":
            if payload["notice_type"] == "no_reply":
                await supports_svc.mark_no_reply_notified(session, payload["call_id"])
            else:
                await supports_svc.mark_pending_notified(session, payload["call_id"])


async def _process_one(bot: Bot, limiter: TokenBucket) -> bool:
    """Claims and resolves one due row. Returns False if the queue was empty
    (nothing to do), True otherwise (sent, rescheduled, or terminally
    failed) — the caller uses this to decide whether to idle-wait."""
    async with session_scope() as session:
        row = await outbox_svc.claim_next(session)
        if row is None:
            return False
        await limiter.acquire()
        chat_id, payload, row_id = row.chat_id, row.payload_json, row.id
        markup = _load_markup(payload.get("reply_markup"))
        try:
            msg = await bot.send_message(
                chat_id, payload.get("text", ""),
                reply_markup=markup, parse_mode=payload.get("parse_mode"),
            )
        except TelegramRetryAfter as e:
            log.info("outbox: RetryAfter %ss on row %s, rescheduling", e.retry_after, row_id)
            await outbox_svc.reschedule(
                session, row,
                retry_at=utc_now() + timedelta(seconds=e.retry_after),
                error=f"RetryAfter {e.retry_after}s",
            )
            return True
        except (TelegramForbiddenError, TelegramNotFound) as e:
            if payload.get("kind") == "summary":
                # Admin auto-summary: "blocked"/"not found" is very often just
                # "hasn't pressed /start yet" at pilot scale, not permanent —
                # retry with backoff like a generic failure instead of giving
                # up instantly (matches the pre-outbox behavior's explicit
                # intent, see _maybe_send_summary), with SUMMARY_MAX_ATTEMPTS'
                # extended budget so it survives the whole open-survey window.
                went_terminal = await outbox_svc.record_failure(
                    session, row, str(e), max_attempts=SUMMARY_MAX_ATTEMPTS
                )
                if went_terminal:
                    await _on_resolved(session, payload, "terminal_failed", None, chat_id)
                return True
            # Permanent — blocked bot / chat gone. No point retrying.
            log.info("outbox: terminal delivery failure for row %s: %s", row_id, e)
            await outbox_svc.mark_terminal(session, row, str(e))
            await _on_resolved(session, payload, "terminal_failed", None, chat_id)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("outbox: send failed for row %s: %s", row_id, e)
            max_attempts = (
                SUMMARY_MAX_ATTEMPTS
                if payload.get("kind") == "summary"
                else outbox_svc.MAX_ATTEMPTS
            )
            went_terminal = await outbox_svc.record_failure(
                session, row, str(e), max_attempts=max_attempts
            )
            if went_terminal:
                await _on_resolved(session, payload, "terminal_failed", None, chat_id)
            return True
        await outbox_svc.mark_sent(session, row, msg.message_id)
        await _on_resolved(session, payload, "sent", msg.message_id, chat_id)
    return True


async def run_sender_worker(bot: Bot, stop_event: asyncio.Event) -> None:
    """Runs until `stop_event` is set. Idles (checking `stop_event` at least
    every `POLL_INTERVAL`s) when the queue is empty instead of busy-polling."""
    limiter = TokenBucket(RATE_PER_SECOND)
    while not stop_event.is_set():
        try:
            did_work = await _process_one(bot, limiter)
        except Exception:  # noqa: BLE001
            log.exception("outbox: unexpected error processing a row")
            did_work = True  # avoid a tight error loop turning into idle-sleep starvation
        if not did_work:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL)
            except TimeoutError:
                pass
