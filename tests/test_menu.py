"""Tests for role-aware /start, /grant, and the student home screen."""
from types import SimpleNamespace

from app.bot.handlers.admin import cmd_grant, cmd_start
from app.bot.handlers.student import show_student_home
from app.core.config import settings
from app.db.session import session_scope
from app.services import admins, groups, persons


class FakeMessage:
    def __init__(self, user_id: int, full_name: str = "Юзер") -> None:
        self.from_user = SimpleNamespace(
            id=user_id, full_name=full_name, username="user"
        )
        self.replies: list[str] = []
        self.markups: list = []

    async def answer(self, text: str, **kwargs) -> None:
        self.replies.append(text)
        self.markups.append(kwargs.get("reply_markup"))


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs) -> None:
        self.sent.append((chat_id, text))


class FakeDialogManager:
    """Заглушка для тестов /start — реальный aiogram-dialog не нужен в этих
    ветках (teacher is None / student / уже есть школа), но cmd_start требует
    позиционный dialog_manager."""

    async def start(self, *args, **kwargs) -> None:
        pass


# ── /start role logic ────────────────────────────────────────────────


async def test_start_new_user_is_not_teacher(db) -> None:
    msg = FakeMessage(user_id=10)
    await cmd_start(msg, FakeDialogManager())
    async with session_scope() as s:
        assert await admins.get_by_tg_id(s, 10) is None  # not auto-promoted
    assert any("ссылк" in r.lower() for r in msg.replies)  # guest view


async def test_start_developer_becomes_teacher(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "developer_id", 777)
    msg = FakeMessage(user_id=777)
    await cmd_start(msg, FakeDialogManager())
    async with session_scope() as s:
        assert await admins.get_by_tg_id(s, 777) is not None
    assert msg.markups[-1] is not None  # teacher menu keyboard


async def test_start_known_teacher_shows_menu(db) -> None:
    async with session_scope() as s:
        await admins.get_or_create(s, 10, "t", "Препод")
    msg = FakeMessage(user_id=10)
    await cmd_start(msg, FakeDialogManager())
    assert msg.markups[-1] is not None


async def test_start_student_sees_home_not_teacher(db) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 1, "t", "Препод")
        g = await groups.create_group(s, t.id, "Сальса", "0", "19:00", 18)
        await persons.add_to_group(s, g, 500, "u", "Аня")
    msg = FakeMessage(user_id=500)
    await cmd_start(msg, FakeDialogManager())
    assert any("Сальса" in r for r in msg.replies)
    async with session_scope() as s:
        assert await admins.get_by_tg_id(s, 500) is None


# ── /grant ───────────────────────────────────────────────────────────


async def test_grant_makes_teacher(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "developer_id", 777)
    bot = FakeBot()
    await cmd_grant(FakeMessage(777), SimpleNamespace(args="555"), bot)
    async with session_scope() as s:
        assert await admins.get_by_tg_id(s, 555) is not None
    assert any(chat == 555 for chat, _ in bot.sent)  # new teacher notified


async def test_grant_ignored_for_non_developer(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "developer_id", 777)
    bot = FakeBot()
    await cmd_grant(FakeMessage(999), SimpleNamespace(args="555"), bot)
    async with session_scope() as s:
        assert await admins.get_by_tg_id(s, 555) is None


async def test_grant_already_teacher(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "developer_id", 777)
    async with session_scope() as s:
        await admins.get_or_create(s, 555, "t", "Уже Препод")
    msg = FakeMessage(777)
    await cmd_grant(msg, SimpleNamespace(args="555"), FakeBot())
    assert any("уже" in r.lower() for r in msg.replies)


# ── student home ─────────────────────────────────────────────────────


async def test_student_home_lists_groups(db) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 1, "t", "Препод")
        g = await groups.create_group(s, t.id, "Бачата", "0,2", "19:00", 18)
        await persons.add_to_group(s, g, 500, "u", "Аня")
    msg = FakeMessage(user_id=500)
    await show_student_home(msg)
    assert any("Бачата" in r for r in msg.replies)


async def test_student_home_unknown_shows_own_id(db) -> None:
    msg = FakeMessage(user_id=999)
    await show_student_home(msg)
    assert any("999" in r for r in msg.replies)  # own ID shown to a guest
