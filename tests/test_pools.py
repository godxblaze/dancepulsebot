"""Tests for the teacher's support pool with skill levels."""
from types import SimpleNamespace

from app.bot.handlers.teacher import cb_pool_toggle, cmd_pool
from app.db.models import Person
from app.db.session import session_scope
from app.services import groups, persons, teachers


class FakeCbMessage:
    def __init__(self) -> None:
        self.markup_edits = 0

    async def edit_reply_markup(self, **kwargs) -> None:
        self.markup_edits += 1


class FakeCallback:
    def __init__(self, data: str, user_id: int) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeCbMessage()
        self.answers: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)


class FakeMessage:
    def __init__(self, user_id: int) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.replies: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.replies.append(text)


async def _person_of_teacher(
    teacher_tg: int = 100, person_tg: int = 500, name: str = "Аня"
) -> tuple[int, int]:
    async with session_scope() as s:
        t = await teachers.get_or_create(s, teacher_tg, "t", "Препод")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        p, _ = await persons.add_to_group(s, g, person_tg, "u", name)
        return t.id, p.id


async def test_cmd_pool_empty(db) -> None:
    msg = FakeMessage(user_id=100)
    await cmd_pool(msg)
    assert msg.replies and "пуст" in msg.replies[0]


async def test_cmd_pool_lists_people(db) -> None:
    await _person_of_teacher()
    msg = FakeMessage(user_id=100)
    await cmd_pool(msg)
    assert any("пул саппортов" in r.lower() for r in msg.replies)


async def test_pool_toggle_cycles_level(db) -> None:
    _, pid = await _person_of_teacher()
    # tap cycles the level: None -> 1 -> 2 -> 3 -> None
    for expected in (1, 2, 3, None):
        await cb_pool_toggle(FakeCallback(f"tpool:{pid}", user_id=100))
        async with session_scope() as s:
            person = await s.get(Person, pid)
            assert person is not None and person.support_level == expected


async def test_pool_toggle_rejects_foreign_person(db) -> None:
    _, pid = await _person_of_teacher(teacher_tg=100, person_tg=500)
    async with session_scope() as s:
        await teachers.get_or_create(s, 200, "x", "Чужой Препод")
    await cb_pool_toggle(FakeCallback(f"tpool:{pid}", user_id=200))
    async with session_scope() as s:
        person = await s.get(Person, pid)
        assert person is not None and person.support_level is None  # unchanged
