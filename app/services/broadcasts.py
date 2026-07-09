"""Broadcast entity — the parent record for a broadcast run. Per-recipient
delivery state lives in `message_outbox` rows (`broadcast_id` FK), queried
here for progress reporting.

`app/bot/dialogs/misc_dialogs.py`'s group broadcast (Phase 5) and Phase 11's
global developer broadcast both create rows through this module — one
broadcast mechanism, not two, per the task's explicit guardrail.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Broadcast, MessageOutbox

# Все статусы message_outbox, которые в принципе бывают у broadcast-строки —
# используется чтобы progress() всегда возвращал полный набор ключей (0, если
# строк в этом статусе нет), а не только те, что реально встретились.
_STATUSES = ("pending", "sent", "terminal_failed")


async def create_broadcast(
    session: AsyncSession,
    *,
    scope_type: str,
    scope_payload: dict,
    audience_type: str,
    text: str,
    created_by_admin_id: int | None,
) -> Broadcast:
    broadcast = Broadcast(
        scope_type=scope_type,
        scope_payload_json=scope_payload,
        audience_type=audience_type,
        text=text,
        created_by_admin_id=created_by_admin_id,
    )
    session.add(broadcast)
    await session.flush()
    return broadcast


async def progress(session: AsyncSession, broadcast_id: int) -> dict[str, int]:
    """{"pending": N, "sent": N, "terminal_failed": N, "total": N} for this
    broadcast's outbox rows."""
    rows = (
        await session.execute(
            select(MessageOutbox.status, func.count())
            .where(MessageOutbox.broadcast_id == broadcast_id)
            .group_by(MessageOutbox.status)
        )
    ).all()
    counts = dict.fromkeys(_STATUSES, 0)
    for status, n in rows:
        counts[status] = n
    counts["total"] = sum(counts.values())
    return counts
