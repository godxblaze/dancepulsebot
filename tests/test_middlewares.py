"""ClearStaleCancelState — menu-кнопка чистит зависший CancelLesson.reason;
несвязанные state/текст остаются нетронутыми."""
from app.bot.middlewares import ClearStaleCancelState
from app.bot.states import CancelLesson


class _FSM:
    def __init__(self, state=None) -> None:
        self._state = state
        self.cleared = False

    async def get_state(self):
        return self._state

    async def clear(self) -> None:
        self.cleared = True
        self._state = None


class _Msg:
    def __init__(self, text: str) -> None:
        self.text = text


async def _noop_handler(event, data) -> str:
    return "handled"


async def test_menu_text_clears_stale_cancel_state() -> None:
    mw = ClearStaleCancelState()
    fsm = _FSM(state=CancelLesson.reason.state)
    result = await mw(_noop_handler, _Msg("🏫 Школа"), {"state": fsm})
    assert fsm.cleared is True
    assert result == "handled"  # хендлер всё равно вызван — экран открывается


async def test_menu_text_leaves_unrelated_state_untouched() -> None:
    mw = ClearStaleCancelState()
    fsm = _FSM(state="SomeOtherFlow:step")
    await mw(_noop_handler, _Msg("🏫 Школа"), {"state": fsm})
    assert fsm.cleared is False


async def test_non_menu_text_does_not_touch_cancel_state() -> None:
    """Обычный текст (напр. причина отмены, набираемая в «Другое») не задет —
    иначе middleware сама бы стирала state посреди легитимного флоу."""
    mw = ClearStaleCancelState()
    fsm = _FSM(state=CancelLesson.reason.state)
    await mw(_noop_handler, _Msg("Зал занят"), {"state": fsm})
    assert fsm.cleared is False


async def test_missing_state_in_data_is_safe() -> None:
    mw = ClearStaleCancelState()
    result = await mw(_noop_handler, _Msg("🏫 Школа"), {})
    assert result == "handled"
