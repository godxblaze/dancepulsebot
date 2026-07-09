"""Test helper: drain the outbox through the real sender-worker code path
(not a mock) against a FakeBot, so tests written against `bot.sent` before
Phase 4's outbox migration keep working with one extra line."""
from app.bot.outbox_worker import TokenBucket, _process_one


async def drain_outbox(bot, *, rate: float = 10_000) -> int:
    """Processes every due outbox row via `_process_one` until the queue is
    empty. High default rate keeps tests fast — throttling itself is covered
    by `tests/test_outbox.py`."""
    limiter = TokenBucket(rate=rate, capacity=rate)
    n = 0
    while await _process_one(bot, limiter):
        n += 1
    return n
