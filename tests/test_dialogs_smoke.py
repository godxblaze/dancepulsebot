"""Smoke-проверки для всех зарегистрированных aiogram-dialog'ов.

Цели: (1) dialog'и импортятся и строятся без исключений; (2) у всех Button и
Select сигнатура on_click имеет нужное число параметров; (3) у TextInput
сигнатура on_success тоже.

Не запускает runtime aiogram-dialog (это тестируется upstream'ом), но ловит
очевидные опечатки сразу: handler с тремя позиционными параметрами вместо
четырёх упадёт в проде при первом клике.
"""
from __future__ import annotations

import inspect

from aiogram_dialog import Dialog
from aiogram_dialog.widgets.input import TextInput
from aiogram_dialog.widgets.kbd import Button, Multiselect, Select


def _walk_widgets(obj, found: list) -> None:
    """Рекурсивный обход атрибутов dialog/window/widget — собирает все Button,
    Select, Multiselect, TextInput. aiogram-dialog не expose'ит публичного
    walker'а, поэтому идём через __dict__."""
    seen = id(obj)
    if seen in found_ids:
        return
    found_ids.add(seen)
    if isinstance(obj, Button | Select | Multiselect | TextInput):
        found.append(obj)
    for attr in vars(obj).values() if hasattr(obj, "__dict__") else ():
        if isinstance(attr, list | tuple):
            for el in attr:
                _walk_widgets(el, found)
        elif hasattr(attr, "__dict__"):
            _walk_widgets(attr, found)


found_ids: set[int] = set()


def _collect(dialog: Dialog) -> list:
    found: list = []
    global found_ids
    found_ids = set()
    _walk_widgets(dialog, found)
    return found


def _param_count(fn) -> int:
    """Считает все позиционные параметры (включая обычные)."""
    sig = inspect.signature(fn)
    return sum(
        1 for p in sig.parameters.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    )


def test_all_dialogs_import() -> None:
    """Все 7 dialog'ов должны импортироваться без исключений."""
    from app.bot.dialogs import (
        broadcast_dialog,
        create_school_dialog,
        dev_broadcast_dialog,
        edit_group_dialog,
        edit_name_dialog,
        new_group_dialog,
        rename_school_dialog,
    )
    for d in (
        new_group_dialog, edit_group_dialog, create_school_dialog,
        rename_school_dialog, edit_name_dialog, broadcast_dialog, dev_broadcast_dialog,
    ):
        assert isinstance(d, Dialog)


def test_button_on_click_signatures() -> None:
    """Button.on_click должен принимать ровно 3 позиционных: callback, button, manager."""
    from app.bot.dialogs import (
        broadcast_dialog,
        create_school_dialog,
        dev_broadcast_dialog,
        edit_group_dialog,
        edit_name_dialog,
        new_group_dialog,
        rename_school_dialog,
    )
    bad: list[str] = []
    for d in (
        new_group_dialog, edit_group_dialog, create_school_dialog,
        rename_school_dialog, edit_name_dialog, broadcast_dialog, dev_broadcast_dialog,
    ):
        for w in _collect(d):
            if not isinstance(w, Button) or isinstance(w, Select | Multiselect):
                continue
            cb_attr = getattr(w, "on_click", None)
            fn = getattr(cb_attr, "callback", None) if cb_attr is not None else None
            if fn is None or not callable(fn):
                continue
            n = _param_count(fn)
            if n != 3:
                bad.append(f"{fn.__module__}.{fn.__name__}: {n} positional (expected 3)")
    assert not bad, "Bad Button.on_click signatures:\n  " + "\n  ".join(bad)


def test_select_on_click_signatures() -> None:
    """Select.on_click должен принимать 4 позиционных: callback, widget, manager, item_id."""
    from app.bot.dialogs import (
        edit_group_dialog,
        new_group_dialog,
    )
    bad: list[str] = []
    for d in (new_group_dialog, edit_group_dialog):
        for w in _collect(d):
            if not isinstance(w, Select) or isinstance(w, Multiselect):
                continue
            cb_attr = getattr(w, "on_click", None)
            fn = getattr(cb_attr, "callback", None) if cb_attr is not None else None
            if fn is None or not callable(fn):
                continue
            n = _param_count(fn)
            if n != 4:
                bad.append(f"{fn.__module__}.{fn.__name__}: {n} positional (expected 4)")
    assert not bad, "Bad Select.on_click signatures:\n  " + "\n  ".join(bad)


def test_text_input_on_success_signatures() -> None:
    """TextInput.on_success должен принимать 4 позиционных: message, widget, manager, value."""
    from app.bot.dialogs import (
        broadcast_dialog,
        create_school_dialog,
        dev_broadcast_dialog,
        edit_group_dialog,
        edit_name_dialog,
        new_group_dialog,
        rename_school_dialog,
    )
    bad: list[str] = []
    for d in (
        new_group_dialog, edit_group_dialog, create_school_dialog,
        rename_school_dialog, edit_name_dialog, broadcast_dialog, dev_broadcast_dialog,
    ):
        for w in _collect(d):
            if not isinstance(w, TextInput):
                continue
            cb_attr = getattr(w, "on_success", None)
            fn = getattr(cb_attr, "callback", None) if cb_attr is not None else None
            if fn is None or not callable(fn):
                continue
            n = _param_count(fn)
            if n != 4:
                bad.append(f"{fn.__module__}.{fn.__name__}: {n} positional (expected 4)")
    assert not bad, "Bad TextInput.on_success signatures:\n  " + "\n  ".join(bad)


def test_windows_rendering_escaped_html_declare_parse_mode() -> None:
    """Getter'ы этих окон отдают html.escape()'нутые строки ({title}/{summary}/
    {current}) — без parse_mode=HTML на самом Window (ни Bot, ни Window не
    заданы с HTML-дефолтом) html-сущности типа &amp; показывались бы юзеру
    буквально, а не декодировались обратно в спецсимволы."""
    from aiogram.enums import ParseMode

    from app.bot.dialogs.edit_group import edit_group_dialog
    from app.bot.dialogs.misc_dialogs import edit_name_dialog
    from app.bot.dialogs.states import EditGroupSG, EditNameSG

    assert edit_group_dialog.windows[EditGroupSG.menu].parse_mode == ParseMode.HTML
    assert edit_name_dialog.windows[EditNameSG.name].parse_mode == ParseMode.HTML
