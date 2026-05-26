"""Tests for the join / onboarding flow and QR generation."""
from types import SimpleNamespace

from app.bot.handlers.student import (
    join_gender,
    join_name_confirm,
    join_name_typed,
    join_start,
)
from app.bot.handlers.admin import cb_group_invite
from app.bot.states import JoinFlow
from app.bot.utils import qr_png
from app.db.models import Gender
from app.db.session import session_scope
from app.services import groups, persons, admins

# ── fakes ────────────────────────────────────────────────────────────


class FakeMessage:
    def __init__(self, user_id, full_name="Аня Иванова", username="anya", text=None):
        self.from_user = SimpleNamespace(
            id=user_id, full_name=full_name, username=username
        )
        self.text = text
        self.replies: list[str] = []

    async def answer(self, text, **kwargs) -> None:
        self.replies.append(text)


class FakeCbMessage:
    def __init__(self) -> None:
        self.text: str | None = None
        self.photo_captions: list[str] = []

    async def edit_text(self, text, **kwargs) -> None:
        self.text = text

    async def answer_photo(self, photo, caption=None, **kwargs) -> None:
        self.photo_captions.append(caption or "")


class FakeCallback:
    def __init__(self, data, user_id, full_name="Аня Иванова", username="anya"):
        self.data = data
        self.from_user = SimpleNamespace(
            id=user_id, full_name=full_name, username=username
        )
        self.message = FakeCbMessage()
        self.answers: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs) -> None:
        self.sent.append((chat_id, text))


class FakeFSM:
    def __init__(self) -> None:
        self.state = None
        self.data: dict = {}

    async def set_state(self, state) -> None:
        self.state = state

    async def get_data(self) -> dict:
        return dict(self.data)

    async def update_data(self, **kwargs) -> dict:
        self.data.update(kwargs)
        return dict(self.data)

    async def clear(self) -> None:
        self.state = None
        self.data = {}


async def _teacher_and_group(name: str = "Сальса", weekdays: str = "0"):
    async with session_scope() as s:
        t = await admins.get_or_create(s, 100, "teach", "Препод")
        g = await groups.create_group(s, t.id, name, weekdays, "19:00", 18)
        return t.id, g.id, g.join_code


# ── join flow ────────────────────────────────────────────────────────


async def test_join_new_person_full_flow(db) -> None:
    tid, gid, code = await _teacher_and_group()
    fsm, bot = FakeFSM(), FakeBot()

    msg = FakeMessage(user_id=500, full_name="Аня Иванова")
    await join_start(msg, SimpleNamespace(args=f"join_{code}"), fsm, bot)
    assert fsm.state == JoinFlow.name
    assert any("Как тебя зовут" in r for r in msg.replies)

    # accept the suggested telegram name
    name_call = FakeCallback("join:name_ok", user_id=500, full_name="Аня Иванова")
    await join_name_confirm(name_call, fsm)
    assert fsm.state == JoinFlow.gender

    # pick gender -> finalize
    gender_call = FakeCallback("join:gender:female", user_id=500)
    await join_gender(gender_call, fsm, bot)
    assert fsm.state is None
    assert "Готово" in (gender_call.message.text or "")

    async with session_scope() as s:
        p = await persons.get_person(s, tid, 500)
        assert p is not None
        assert p.full_name == "Аня Иванова" and p.gender == Gender.female
        assert await persons.count_members(s, gid) == 1
    assert any(chat == 100 for chat, _ in bot.sent)  # teacher notified


async def test_join_typed_custom_name(db) -> None:
    tid, _, code = await _teacher_and_group()
    fsm, bot = FakeFSM(), FakeBot()
    await join_start(FakeMessage(500), SimpleNamespace(args=f"join_{code}"), fsm, bot)

    typed = FakeMessage(user_id=500, text="Анечка")
    await join_name_typed(typed, fsm)
    assert fsm.state == JoinFlow.gender

    await join_gender(FakeCallback("join:gender:male", 500), fsm, bot)
    async with session_scope() as s:
        p = await persons.get_person(s, tid, 500)
        assert p is not None
        assert p.full_name == "Анечка" and p.gender == Gender.male


async def test_join_unknown_code(db) -> None:
    fsm, bot = FakeFSM(), FakeBot()
    msg = FakeMessage(500)
    await join_start(msg, SimpleNamespace(args="join_nope"), fsm, bot)
    assert any("не найдена" in r for r in msg.replies)
    assert fsm.state is None  # no onboarding started


async def test_join_already_member(db) -> None:
    _, _, code = await _teacher_and_group()
    async with session_scope() as s:
        g = await groups.get_by_join_code(s, code)
        await persons.add_to_group(s, g, 500, "u", "Аня")
    fsm, bot = FakeFSM(), FakeBot()
    msg = FakeMessage(500)
    await join_start(msg, SimpleNamespace(args=f"join_{code}"), fsm, bot)
    assert any("уже в группе" in r for r in msg.replies)
    assert fsm.state is None


async def test_join_returning_person_skips_onboarding(db) -> None:
    """A known person (gender already set) joining another group: no questions."""
    async with session_scope() as s:
        t = await admins.get_or_create(s, 100, "teach", "Препод")
        g1 = await groups.create_group(s, t.id, "G1", "0", "19:00", 18)
        g2 = await groups.create_group(s, t.id, "G2", "2", "20:00", 18)
        p, _ = await persons.add_to_group(s, g1, 500, "u", "Аня")
        await persons.set_gender(s, p.id, Gender.female)
        code2, g2id = g2.join_code, g2.id
    fsm, bot = FakeFSM(), FakeBot()
    msg = FakeMessage(500)
    await join_start(msg, SimpleNamespace(args=f"join_{code2}"), fsm, bot)
    assert fsm.state is None  # fast path, no FSM
    assert any("Записал" in r for r in msg.replies)
    async with session_scope() as s:
        assert await persons.count_members(s, g2id) == 1
    assert any(chat == 100 for chat, _ in bot.sent)


async def test_join_known_person_without_gender_runs_onboarding(db) -> None:
    """Person known to the bot but with no gender set still goes through onboarding."""
    async with session_scope() as s:
        t = await admins.get_or_create(s, 100, "teach", "Препод")
        g1 = await groups.create_group(s, t.id, "G1", "0", "19:00", 18)
        g2 = await groups.create_group(s, t.id, "G2", "2", "20:00", 18)
        await persons.add_to_group(s, g1, 500, "u", "Старое Имя")  # no gender set
        code2, tid = g2.join_code, t.id
    fsm, bot = FakeFSM(), FakeBot()
    await join_start(FakeMessage(500), SimpleNamespace(args=f"join_{code2}"), fsm, bot)
    assert fsm.state == JoinFlow.name  # onboarding, not the fast path

    await join_name_typed(FakeMessage(500, text="Новое Имя"), fsm)
    await join_gender(FakeCallback("join:gender:female", 500), fsm, bot)
    async with session_scope() as s:
        p = await persons.get_person(s, tid, 500)
        assert p is not None
        assert p.gender == Gender.female
        assert p.full_name == "Новое Имя"  # FSM-entered name overwrites the old one


async def test_join_name_typed_rejects_empty(db) -> None:
    _, _, code = await _teacher_and_group()
    fsm, bot = FakeFSM(), FakeBot()
    await join_start(FakeMessage(500), SimpleNamespace(args=f"join_{code}"), fsm, bot)
    typed = FakeMessage(500, text="   ")
    await join_name_typed(typed, fsm)
    assert fsm.state == JoinFlow.name  # not advanced
    assert any("пустым" in r for r in typed.replies)


# ── QR ───────────────────────────────────────────────────────────────


def test_qr_png_is_valid_png() -> None:
    png = qr_png("https://t.me/dancepulsebot?start=join_ABC123")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"  # PNG signature
    assert len(png) > 100


async def test_cb_group_invite_sends_photo(db, monkeypatch) -> None:
    _, gid, _ = await _teacher_and_group()

    async def fake_username(bot) -> str:
        return "dancepulsebot"

    monkeypatch.setattr("app.bot.handlers.admin.get_bot_username", fake_username)
    call = FakeCallback(f"grpinv:{gid}", user_id=100)
    await cb_group_invite(call, None)
    assert call.message.photo_captions
    assert "Сальса" in call.message.photo_captions[0]
