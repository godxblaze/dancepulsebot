"""Phase 4 (B16): UI выбора режима автодобора в /pool — клавиатура + хэндлер."""
from types import SimpleNamespace

from app.bot.handlers.pool import cb_cfg_mode
from app.bot.keyboards import recruit_config_keyboard
from app.db.session import session_scope
from app.services import admins as admins_svc


class FakeCbMessage:
    def __init__(self) -> None:
        self.text: str | None = None
        self.reply_markup = None
        self.parse_mode = None

    async def edit_text(self, text, **kwargs) -> None:
        self.text = text
        self.reply_markup = kwargs.get("reply_markup")
        self.parse_mode = kwargs.get("parse_mode")


class FakeCallback:
    def __init__(self, data: str, user_id: int) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeCbMessage()
        self.answers: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)


def _mode_buttons(markup):
    return [
        b
        for row in markup.inline_keyboard
        for b in row
        if (b.callback_data or "").startswith("rcfgmode:")
    ]


def test_recruit_config_keyboard_has_four_modes_current_marked() -> None:
    kbd = recruit_config_keyboard(2, "19:00", 3, "auto_top")
    btns = _mode_buttons(kbd)
    assert len(btns) == 4
    assert {b.callback_data for b in btns} == {
        "rcfgmode:curated",
        "rcfgmode:open_race",
        "rcfgmode:auto_top",
        "rcfgmode:escalate",
    }
    marked = [b for b in btns if b.text.startswith("✅")]
    assert len(marked) == 1 and marked[0].callback_data == "rcfgmode:auto_top"


async def test_cb_cfg_mode_changes_and_rerenders(db) -> None:
    async with session_scope() as s:
        await admins_svc.get_or_create(s, 100, "t", "Препод")
    cb = FakeCallback("rcfgmode:open_race", 100)
    await cb_cfg_mode(cb)
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, 100)
        assert teacher.recruit_mode == "open_race"
    # карточка ре-рендерилась с человеческим описанием режима
    assert cb.message.text is not None and "Режим" in cb.message.text
    marked = [b for b in _mode_buttons(cb.message.reply_markup) if b.text.startswith("✅")]
    assert len(marked) == 1 and marked[0].callback_data == "rcfgmode:open_race"


async def test_cb_cfg_mode_invalid_value_ignored(db) -> None:
    async with session_scope() as s:
        await admins_svc.get_or_create(s, 100, "t", "Препод")
    cb = FakeCallback("rcfgmode:bogus", 100)
    await cb_cfg_mode(cb)  # не падает
    async with session_scope() as s:
        teacher = await admins_svc.get_by_tg_id(s, 100)
        assert teacher.recruit_mode == "curated"  # значение не изменилось
    assert cb.message.text is None  # ре-рендера не было (без изменений)
