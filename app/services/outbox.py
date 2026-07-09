"""Durable send queue — `message_outbox` enqueue/claim/resolve helpers.

Used by scheduler.py/lesson_cancel.py/recruit.py's send loops (Phase 4) and
the sender worker (`app/bot/outbox_worker.py`).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageOutbox
from app.services.schedule import utc_now


def build_payload(text: str, *, reply_markup=None, parse_mode: str | None = None, **extra) -> dict:
    """Build a JSON-serializable send payload for `enqueue()`. `reply_markup`
    is any aiogram markup object (or None) — serialized here (pydantic
    `model_dump`) so call sites don't need to know how the sender worker will
    reconstruct it. `**extra` carries kind-specific fields the worker's
    post-delivery hook needs (survey_id, person_id, call_id, ...)."""
    payload: dict = {"text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup.model_dump(mode="json")
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    payload.update(extra)
    return payload

# После этого числа неудачных попыток отправки строка помечается
# terminal_failed и больше не забирается dequeue'ом. 5 — достаточно, чтобы
# пережить транзиентные сетевые сбои/недолгие тайм-ауты Telegram API, но не
# держать вечно то, что реально недоставимо (юзер заблокировал бота и т.п.).
MAX_ATTEMPTS = 5


async def enqueue(
    session: AsyncSession,
    business_key: str,
    chat_id: int,
    payload: dict,
    *,
    priority: int = 0,
    scheduled_at: datetime | None = None,
    broadcast_id: int | None = None,
) -> None:
    """Insert-or-skip on `business_key` — re-enqueueing the same key is a
    no-op, not a duplicate or an error. Uses the caller's session/transaction
    (never opens its own `session_scope()` — see `[[pilot-deferred-list-closed-2026-07-06]]`
    nested-session gotcha). `broadcast_id` tags rows belonging to a
    `Broadcast` run (Phase 5) for the progress query."""
    stmt = (
        pg_insert(MessageOutbox)
        .values(
            business_key=business_key,
            chat_id=chat_id,
            payload_json=payload,
            priority=priority,
            scheduled_at=scheduled_at or utc_now(),
            broadcast_id=broadcast_id,
        )
        .on_conflict_do_nothing(index_elements=["business_key"])
    )
    await session.execute(stmt)


async def claim_next(session: AsyncSession) -> MessageOutbox | None:
    """Claims (via `SELECT ... FOR UPDATE SKIP LOCKED`) the single highest-
    priority due row, if any. The row lock is held for the lifetime of the
    caller's transaction — the caller MUST send/resolve and commit before
    another claimer can see this row again, which is what makes concurrent
    dequeue safe with zero duplicate delivery."""
    stmt = (
        select(MessageOutbox)
        .where(MessageOutbox.status == "pending", MessageOutbox.scheduled_at <= utc_now())
        .order_by(MessageOutbox.priority.desc(), MessageOutbox.scheduled_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def mark_sent(
    session: AsyncSession, row: MessageOutbox, telegram_message_id: int | None
) -> None:
    row.status = "sent"
    row.sent_at = utc_now()
    row.telegram_message_id = telegram_message_id


async def reschedule(
    session: AsyncSession, row: MessageOutbox, *, retry_at: datetime, error: str
) -> None:
    """Transient failure (e.g. `TelegramRetryAfter`) — push `scheduled_at`
    into the future, stays `pending`, does NOT count toward `MAX_ATTEMPTS`
    (Telegram asking us to slow down isn't our failure)."""
    row.scheduled_at = retry_at
    row.last_error = error[:2000]


async def mark_terminal(session: AsyncSession, row: MessageOutbox, error: str) -> None:
    """Immediate give-up, no retry/backoff — for permanent delivery failures
    (blocked bot, chat gone) where retrying would just waste attempts on
    something that will never succeed."""
    row.status = "terminal_failed"
    row.attempts += 1
    row.last_error = error[:2000]


async def record_failure(
    session: AsyncSession, row: MessageOutbox, error: str, *, max_attempts: int = MAX_ATTEMPTS
) -> bool:
    """Genuine (possibly transient) send failure — increments `attempts`;
    past `max_attempts` transitions to `terminal_failed` and stops retrying,
    otherwise stays `pending` with exponential backoff. Returns True iff this
    call just transitioned the row to `terminal_failed` (callers use this to
    decide whether to fire terminal-outcome bookkeeping).

    `max_attempts` is overridable per call site — see
    `app.bot.outbox_worker.SUMMARY_MAX_ATTEMPTS` for why auto-summaries need
    a much larger budget than the generic default."""
    row.attempts += 1
    row.last_error = error[:2000]
    if row.attempts >= max_attempts:
        row.status = "terminal_failed"
        return True
    row.scheduled_at = utc_now() + _backoff(row.attempts)
    return False


def _backoff(attempts: int) -> timedelta:
    """Exponential, capped at 5 minutes: 2s, 4s, 8s, 16s, 32s, ... ≤300s."""
    return timedelta(seconds=min(300, 2**attempts))
