"""Tests for the role-aware /start and the student home screen."""
from types import SimpleNamespace

from app.bot.handlers.student import show_student_home
from app.bot.handlers.teacher import cmd_start
from app.db.session import session_scope
from app.services import groups, persons, teachers


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


async def test_start_new_user_becomes_teacher(db) -> None:
    msg = FakeMessage(user_id=10, full_name="Новый Препод")
    await cmd_start(msg)
    async with session_scope() as s:
        assert await teachers.get_by_tg_id(s, 10) is not None
    assert any("Dance Pulse" in r for r in msg.replies)
    assert msg.markups[-1] is not None  # teacher menu keyboard attached


async def test_start_known_teacher_shows_menu(db) -> None:
    async with session_scope() as s:
        await teachers.get_or_create(s, 10, "t", "Препод")
    msg = FakeMessage(user_id=10)
    await cmd_start(msg)
    assert any("Dance Pulse" in r for r in msg.replies)
    assert msg.markups[-1] is not None  # teacher menu keyboard attached


async def test_start_student_sees_home_not_teacher(db) -> None:
    async with session_scope() as s:
        t = await teachers.get_or_create(s, 1, "t", "Препод")
        g = await groups.create_group(s, t.id, "Сальса", "0", "19:00", 18)
        await persons.add_to_group(s, g, 500, "u", "Аня")
    msg = FakeMessage(user_id=500, full_name="Аня")
    await cmd_start(msg)
    assert any("Сальса" in r for r in msg.replies)  # student home
    async with session_scope() as s:
        assert await teachers.get_by_tg_id(s, 500) is None  # not made a teacher


async def test_student_home_lists_groups(db) -> None:
    async with session_scope() as s:
        t = await teachers.get_or_create(s, 1, "t", "Препод")
        g = await groups.create_group(s, t.id, "Бачата", "0,2", "19:00", 18)
        await persons.add_to_group(s, g, 500, "u", "Аня")
    msg = FakeMessage(user_id=500)
    await show_student_home(msg)
    assert any("Бачата" in r for r in msg.replies)


async def test_student_home_unknown_person(db) -> None:
    msg = FakeMessage(user_id=999)
    await show_student_home(msg)
    assert any("не записан" in r for r in msg.replies)
