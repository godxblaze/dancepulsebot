"""Поиск по имени в списках людей."""
from types import SimpleNamespace

from app.bot.handlers.search import cb_search_start, on_search_query
from app.bot.states import EntitySearch
from app.db.session import session_scope
from app.services import admins, groups, persons


class FakeFSM:
    def __init__(self) -> None:
        self.state = None
        self.data: dict = {}

    async def set_state(self, state) -> None:
        self.state = state

    async def update_data(self, **kw) -> None:
        self.data.update(kw)

    async def get_data(self) -> dict:
        return dict(self.data)

    async def clear(self) -> None:
        self.state = None
        self.data = {}


class FakeMsg:
    def __init__(self, user_id: int, text: str = "") -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.text = text
        self.sent: list[tuple[str, object]] = []
        self.reply_markup = None

    async def answer(self, text: str, reply_markup=None, **kw) -> None:
        self.sent.append((text, reply_markup))

    async def edit_reply_markup(self, reply_markup=None, **kw) -> None:
        self.reply_markup = reply_markup


class FakeCb:
    def __init__(self, data: str, user_id: int) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeMsg(user_id)
        self.answers: list[str] = []

    async def answer(self, text: str = "", **kw) -> None:
        self.answers.append(text)


async def test_pool_search_filters_by_name(db) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 100, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        anna, _ = await persons.add_to_group(s, g, 501, "u", "Анна")
        await persons.add_to_group(s, g, 502, "u", "Борис")
        anna_id = anna.id

    state = FakeFSM()
    await cb_search_start(FakeCb("search:pool", 100), state)
    assert state.state == EntitySearch.query
    assert state.data.get("search_kind") == "pool"

    msg = FakeMsg(100, text="ан")
    await on_search_query(msg, state)
    assert state.state is None  # стейт сброшен
    text, markup = msg.sent[-1]
    cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"tpool:{anna_id}" in " ".join(cbs)  # Анна найдена
    assert all("502" not in c for c in cbs if c.startswith("tpool:"))  # Бориса нет


async def test_pool_search_filter_survives_toggle_tap(db) -> None:
    """Раньше тап по человеку из результата поиска (tpool:) перерисовывал ВЕСЬ
    пул, теряя фильтр — тап должен сохранять тот же отфильтрованный список
    (sticky pool_search_query в FSM, см. pool.py/search.py)."""
    from app.bot.handlers.pool import cb_pool_toggle

    async with session_scope() as s:
        t = await admins.get_or_create(s, 100, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        anna, _ = await persons.add_to_group(s, g, 501, "u", "Анна")
        boris, _ = await persons.add_to_group(s, g, 502, "u", "Борис")
        anna_id, boris_id = anna.id, boris.id

    state = FakeFSM()
    await cb_search_start(FakeCb("search:pool", 100), state)
    msg = FakeMsg(100, text="ан")
    await on_search_query(msg, state)
    assert state.data.get("pool_search_query") == "ан"  # фильтр остался в FSM

    tap = FakeCb(f"tpool:{anna_id}", 100)
    await cb_pool_toggle(tap, state)
    cbs = [
        b.callback_data
        for row in tap.message.reply_markup.inline_keyboard
        for b in row
    ]
    assert any(c.startswith(f"tpool:{anna_id}:") for c in cbs)  # Анна всё ещё видна
    assert not any(c.startswith(f"tpool:{boris_id}:") for c in cbs)  # Борис не вернулся


async def test_search_cancel(db) -> None:
    async with session_scope() as s:
        await admins.get_or_create(s, 100, "t", "T")
    state = FakeFSM()
    await cb_search_start(FakeCb("search:pool", 100), state)
    msg = FakeMsg(100, text="/cancel")
    await on_search_query(msg, state)
    assert state.state is None
    assert "отмен" in msg.sent[-1][0].lower()
