"""Broadcast dialog: даблтап «Отправить» до manager.done() не должен разослать
дважды — guard-флаг в dialog_data вместо module-level Lock (обе таски в одном
event loop'е, флаг ставится до первого await)."""
import asyncio
from types import SimpleNamespace

from app.bot.dialogs.misc_dialogs import _on_broadcast_send
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import groups as groups_svc
from app.services import persons as persons_svc


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

    async def send_message(self, chat_id, text, **kwargs) -> None:
        self.sent.append((chat_id, text))


class FakeManager:
    def __init__(self, dialog_data: dict, bot: FakeBot) -> None:
        self.dialog_data = dialog_data
        self.middleware_data = {"bot": bot}
        self.done_calls = 0

    async def done(self) -> None:
        self.done_calls += 1


async def _group_with_members(n: int) -> int:
    async with session_scope() as s:
        t = await admins_svc.get_or_create(s, 100, "t", "T")
        g = await groups_svc.create_group(s, t.id, "G", "0", "19:00", 18)
        for uid in range(700, 700 + n):
            await persons_svc.add_to_group(s, g, uid, "u", f"P{uid}")
        return g.id


async def test_broadcast_double_tap_sends_once(db) -> None:
    gid = await _group_with_members(3)
    bot = FakeBot()
    manager = FakeManager({"text": "Привет", "group_id": gid}, bot)
    await asyncio.gather(
        _on_broadcast_send(FakeCall(100), None, manager),
        _on_broadcast_send(FakeCall(100), None, manager),
    )
    per_student: dict[int, int] = {}
    for chat, _text in bot.sent:
        per_student[chat] = per_student.get(chat, 0) + 1
    assert len(per_student) == 3  # все трое получили сообщение
    assert all(count == 1 for count in per_student.values())  # ровно по одному
    assert manager.done_calls == 1  # ровно один цикл send'ов дошёл до done()


async def test_broadcast_single_tap_still_sends(db) -> None:
    """Регресс: без даблтапа рассылка по-прежнему работает."""
    gid = await _group_with_members(2)
    bot = FakeBot()
    manager = FakeManager({"text": "Привет", "group_id": gid}, bot)
    await _on_broadcast_send(FakeCall(100), None, manager)
    assert len(bot.sent) == 2
    assert manager.done_calls == 1
