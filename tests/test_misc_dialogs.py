"""Broadcast dialog (Phase 5 — outbox-backed): Broadcast row + per-recipient
outbox rows commit atomically; delivery is async via the sender worker;
double-tap on «Отправить» must create only one Broadcast, not send twice."""
import asyncio
from types import SimpleNamespace

from sqlalchemy import select

from app.bot.dialogs.misc_dialogs import (
    _broadcast_progress_getter,
    _on_broadcast_send,
)
from app.db.models import Broadcast, MessageOutbox
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import broadcasts as broadcasts_svc
from app.services import groups as groups_svc
from app.services import outbox as outbox_svc
from app.services import persons as persons_svc
from tests._outbox_helpers import drain_outbox


class FakeMessage:
    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append(text)


class FakeCall:
    def __init__(self, user_id: int, full_name: str = "Препод") -> None:
        self.from_user = SimpleNamespace(id=user_id, full_name=full_name, username=None)
        self.message = FakeMessage()
        self.answers: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self._mid = 1000

    async def send_message(self, chat_id, text, **kwargs):
        self._mid += 1
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=self._mid)


class FakeManager:
    def __init__(self, dialog_data: dict, bot: FakeBot) -> None:
        self.dialog_data = dialog_data
        self.middleware_data = {"bot": bot}
        self.done_calls = 0
        self.switch_to_calls: list = []

    async def done(self) -> None:
        self.done_calls += 1

    async def switch_to(self, state) -> None:
        self.switch_to_calls.append(state)


async def _group_with_members(n: int) -> int:
    async with session_scope() as s:
        t = await admins_svc.get_or_create(s, 100, "t", "T")
        g = await groups_svc.create_group(s, t.id, "G", "0", "19:00", 18)
        for uid in range(700, 700 + n):
            await persons_svc.add_to_group(s, g, uid, "u", f"P{uid}")
        return g.id


async def _broadcasts_for_group(group_id: int) -> list[Broadcast]:
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(Broadcast).where(
                    Broadcast.scope_payload_json["group_id"].astext == str(group_id)
                )
            )
        ).scalars().all()
        return list(rows)


async def test_broadcast_double_tap_creates_one_broadcast_not_two(db) -> None:
    """dialog_data['sending']'s post-migration role: prevents a double-tap from
    creating TWO Broadcast rows (and double-enqueuing), not from "sending
    twice" — enqueue()'s own idempotency already makes that impossible."""
    gid = await _group_with_members(3)
    bot = FakeBot()
    manager = FakeManager({"text": "Привет", "group_id": gid}, bot)
    await asyncio.gather(
        _on_broadcast_send(FakeCall(100), None, manager),
        _on_broadcast_send(FakeCall(100), None, manager),
    )
    broadcasts = await _broadcasts_for_group(gid)
    assert len(broadcasts) == 1  # exactly one Broadcast, not two
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(MessageOutbox).where(MessageOutbox.broadcast_id == broadcasts[0].id)
            )
        ).scalars().all()
    assert len(rows) == 3  # one outbox row per member, not duplicated
    assert len(manager.switch_to_calls) == 1  # only the winning tap navigated on


async def test_broadcast_single_tap_enqueues_and_delivers(db) -> None:
    """Regression: without a double-tap, the broadcast still enqueues for
    every member and — once drained — actually delivers."""
    gid = await _group_with_members(2)
    bot = FakeBot()
    manager = FakeManager({"text": "Привет", "group_id": gid}, bot)
    await _on_broadcast_send(FakeCall(100), None, manager)

    broadcast_id = manager.dialog_data["broadcast_id"]
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(MessageOutbox).where(MessageOutbox.broadcast_id == broadcast_id)
            )
        ).scalars().all()
    assert len(rows) == 2
    assert all(r.status == "pending" for r in rows)

    await drain_outbox(bot)
    assert len(bot.sent) == 2
    assert all("Привет" in t for _, t in bot.sent)


async def test_broadcast_crash_resume_delivers_only_pending(db) -> None:
    """Phase 5 crash-resume acceptance criterion: a Broadcast with some rows
    already `sent` (before a simulated crash) and others still `pending` —
    running the sender worker fresh must deliver exactly the pending ones and
    must NOT re-send to the already-sent ones."""
    gid = await _group_with_members(4)
    bot = FakeBot()
    manager = FakeManager({"text": "Привет всем", "group_id": gid}, bot)
    await _on_broadcast_send(FakeCall(100), None, manager)
    broadcast_id = manager.dialog_data["broadcast_id"]

    async with session_scope() as s:
        rows = (
            await s.execute(
                select(MessageOutbox).where(MessageOutbox.broadcast_id == broadcast_id)
            )
        ).scalars().all()
        assert len(rows) == 4
        # Simulate a partial send before the "crash": two rows already sent
        # (as if a prior sender-worker run got that far), two still pending.
        already_sent_chat_ids = {rows[0].chat_id, rows[1].chat_id}
        for r in rows[:2]:
            r.status = "sent"
            r.telegram_message_id = 999900 + r.id

    restarted_bot = FakeBot()
    await drain_outbox(restarted_bot)

    delivered_chat_ids = {chat for chat, _ in restarted_bot.sent}
    assert delivered_chat_ids.isdisjoint(already_sent_chat_ids)  # no re-send
    assert len(restarted_bot.sent) == 2  # exactly the 2 pending rows

    async with session_scope() as s:
        rows = (
            await s.execute(
                select(MessageOutbox).where(MessageOutbox.broadcast_id == broadcast_id)
            )
        ).scalars().all()
        assert all(r.status == "sent" for r in rows)


async def test_broadcast_progress_counts_match_outbox_state(db) -> None:
    """Admin-facing progress: pending/sent/blocked counts must match the
    actual outbox row states for this broadcast — verified with a
    mixed-status set of rows."""
    async with session_scope() as s:
        broadcast = await broadcasts_svc.create_broadcast(
            s, scope_type="group", scope_payload={"group_id": 1},
            audience_type="members", text="Test", created_by_admin_id=None,
        )
        broadcast_id = broadcast.id
        for i in range(4):
            key = f"broadcast:{broadcast_id}:{i}"
            await outbox_svc.enqueue(s, key, 1000 + i, {"text": "x"}, broadcast_id=broadcast_id)
        rows = (
            await s.execute(
                select(MessageOutbox).where(MessageOutbox.broadcast_id == broadcast_id)
            )
        ).scalars().all()
        for row, status in zip(rows, ["sent", "sent", "pending", "terminal_failed"], strict=False):
            row.status = status

    async with session_scope() as s:
        counts = await broadcasts_svc.progress(s, broadcast_id)
    assert counts == {"pending": 1, "sent": 2, "terminal_failed": 1, "total": 4}


async def test_broadcast_progress_screen_uses_pulse_card(db) -> None:
    """The rendered progress screen must use app/bot/ui.py primitives (card +
    emoji dict), not ad-hoc formatting — quote the actual text."""
    gid = await _group_with_members(2)
    bot = FakeBot()
    manager = FakeManager({"text": "Всем привет", "group_id": gid}, bot)
    await _on_broadcast_send(FakeCall(100), None, manager)

    screen = await _broadcast_progress_getter(manager)
    text = screen["text"]
    assert text.startswith("📢 <b>Рассылка</b>")  # ui.title(ui.BROADCAST, ...)
    assert "«G»" in text  # ui.card meta — escaped group title
    assert "⏳ в очереди: 2" in text  # ui.PENDING, not yet drained
    assert "✅ доставлено: 0" in text
    assert "всего: 2" in text
