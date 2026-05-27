"""Sandbox: NewGroup wizard на aiogram-dialog.

Цель пробы — оценить миграцию текущего FSM-мастера группы. Этот файл НЕ
подключается в [app/main.py](../app/main.py), он только импортится для
smoke-проверки.

Чтение по результату оценки — в [README.md](README.md).

Тут специально сохранена та же последовательность шагов что в текущем
[app/bot/handlers/admin.py](../app/bot/handlers/admin.py) NewGroup-flow:
  style → level → weekdays → lesson_time → venue → room → teachers_note.
Venue-«new» подшаг и парные «введи сам / выбери из готового» опущены —
сэндбокс смотрит на каркас, не на покрытие 1:1.
"""
from __future__ import annotations

import re

from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Back, Button, Cancel, Multiselect, Select
from aiogram_dialog.widgets.text import Const, Format

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_WD_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


class NewGroupSG(StatesGroup):
    style = State()
    level = State()
    weekdays = State()
    lesson_time = State()
    venue = State()
    room = State()
    teachers = State()
    confirm = State()


async def _styles_getter(dialog_manager: DialogManager, **_: object) -> dict:
    # В реале — `groups_svc.school_styles(s, school_id)`. Сэндбокс отдаёт фиксы.
    return {"styles": [("Бачата", "bachata"), ("Сальса", "salsa"), ("Хип-хоп", "hiphop")]}


async def _weekdays_getter(dialog_manager: DialogManager, **_: object) -> dict:
    return {"days": [(name, str(idx)) for idx, name in enumerate(_WD_NAMES)]}


async def _venues_getter(dialog_manager: DialogManager, **_: object) -> dict:
    # В реале — `_venues_for(s, teacher)`. Сэндбокс — фиксированный список.
    return {"venues": [("Шлюзовая 8с2", "1"), ("Студия Полевик", "2")]}


async def _summary_getter(dialog_manager: DialogManager, **_: object) -> dict:
    data = dialog_manager.dialog_data
    days = sorted(int(d) for d in data.get("weekdays", []))
    return {
        "style": data.get("style") or "—",
        "level": data.get("level") if data.get("level") is not None else "—",
        "weekdays": ", ".join(_WD_NAMES[d] for d in days) if days else "—",
        "lesson_time": data.get("lesson_time") or "—",
        "venue_id": data.get("venue_id") or "—",
        "room": data.get("room") or "—",
        "teachers": data.get("teachers") or "—",
    }


async def _on_style(
    _call: CallbackQuery, _widget: object, manager: DialogManager, item_id: str
) -> None:
    manager.dialog_data["style"] = item_id
    await manager.next()


async def _on_level(
    _call: CallbackQuery, _widget: object, manager: DialogManager, item_id: str
) -> None:
    manager.dialog_data["level"] = int(item_id)
    await manager.next()


async def _on_weekdays_done(
    _call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    ms: Multiselect = manager.find("wd_select")  # type: ignore[assignment]
    selected = ms.get_checked(manager)
    if not selected:
        await _call.answer("Выбери хотя бы один день", show_alert=True)
        return
    manager.dialog_data["weekdays"] = list(selected)
    await manager.next()


async def _on_time(
    message: Message, _widget: MessageInput, manager: DialogManager
) -> None:
    raw = (message.text or "").strip()
    m = _TIME_RE.match(raw)
    if not m:
        await message.answer("Не похоже на время. Формат ЧЧ:ММ, например 19:00")
        return
    manager.dialog_data["lesson_time"] = f"{int(m.group(1)):02d}:{m.group(2)}"
    await manager.next()


async def _on_venue(
    _call: CallbackQuery, _widget: object, manager: DialogManager, item_id: str
) -> None:
    manager.dialog_data["venue_id"] = item_id
    await manager.next()


async def _on_skip_venue(
    _call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    manager.dialog_data["venue_id"] = None
    await manager.next()


async def _on_room(
    message: Message, _widget: MessageInput, manager: DialogManager
) -> None:
    manager.dialog_data["room"] = (message.text or "").strip() or None
    await manager.next()


async def _on_room_skip(
    _call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    manager.dialog_data["room"] = None
    await manager.next()


async def _on_teachers(
    message: Message, _widget: MessageInput, manager: DialogManager
) -> None:
    manager.dialog_data["teachers"] = (message.text or "").strip() or None
    await manager.next()


async def _on_teachers_skip(
    _call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    manager.dialog_data["teachers"] = None
    await manager.next()


async def _on_create(
    call: CallbackQuery, _btn: Button, manager: DialogManager
) -> None:
    # В реале — groups_svc.create_group(...) + send invite QR. Сэндбокс просто
    # завершает диалог.
    await call.answer("OK — здесь был бы create_group()")
    await manager.done()


dialog = Dialog(
    Window(
        Const("Какое направление?"),
        Select(
            Format("{item[0]}"),
            id="style_select",
            item_id_getter=lambda item: item[1],
            items="styles",
            on_click=_on_style,
        ),
        Cancel(Const("⏭ Пропустить")),  # пропуск шага = на confirm
        getter=_styles_getter,
        state=NewGroupSG.style,
    ),
    Window(
        Const("Какой уровень? (0–4)"),
        Select(
            Format("ур.{item}"),
            id="lvl_select",
            item_id_getter=str,
            items=[0, 1, 2, 3, 4],
            on_click=_on_level,
        ),
        Back(Const("◀ Назад")),
        state=NewGroupSG.level,
    ),
    Window(
        Const("Дни занятий — отметь и нажми «Готово»."),
        Multiselect(
            Format("✅ {item[0]}"),
            Format("{item[0]}"),
            id="wd_select",
            item_id_getter=lambda item: item[1],
            items="days",
        ),
        Button(Const("Готово ▶"), id="wd_done", on_click=_on_weekdays_done),
        Back(Const("◀ Назад")),
        getter=_weekdays_getter,
        state=NewGroupSG.weekdays,
    ),
    Window(
        Const("Во сколько начало? Формат ЧЧ:ММ"),
        MessageInput(_on_time),
        Back(Const("◀ Назад")),
        state=NewGroupSG.lesson_time,
    ),
    Window(
        Const("Где проходят занятия?"),
        Select(
            Format("📍 {item[0]}"),
            id="venue_select",
            item_id_getter=lambda item: item[1],
            items="venues",
            on_click=_on_venue,
        ),
        Button(Const("⏭ Пропустить"), id="venue_skip", on_click=_on_skip_venue),
        Back(Const("◀ Назад")),
        getter=_venues_getter,
        state=NewGroupSG.venue,
    ),
    Window(
        Const("Зал? (например «зал 38»)"),
        MessageInput(_on_room),
        Button(Const("⏭ Пропустить"), id="room_skip", on_click=_on_room_skip),
        Back(Const("◀ Назад")),
        state=NewGroupSG.room,
    ),
    Window(
        Const("Кто ведёт? (например «Маша + Петя»)"),
        MessageInput(_on_teachers),
        Button(Const("⏭ Пропустить"), id="teachers_skip", on_click=_on_teachers_skip),
        Back(Const("◀ Назад")),
        state=NewGroupSG.teachers,
    ),
    Window(
        Format(
            "Проверим:\n"
            "• Направление: {style}\n"
            "• Уровень: {level}\n"
            "• Дни: {weekdays}\n"
            "• Время: {lesson_time}\n"
            "• Площадка: {venue_id}\n"
            "• Зал: {room}\n"
            "• Ведут: {teachers}\n\n"
            "Создаём?"
        ),
        Button(Const("✅ Создать"), id="create", on_click=_on_create),
        Back(Const("◀ Назад")),
        getter=_summary_getter,
        state=NewGroupSG.confirm,
    ),
)


__all__ = ["NewGroupSG", "dialog"]


def _smoke() -> None:
    """Smoke-check: модуль импортится, dialog имеет нужный набор windows."""
    assert dialog is not None
    # У aiogram-dialog нет публичного property для списка окон; косвенно — что
    # объект построен без исключений.
    print("aiogram-dialog NewGroup sandbox: OK")


if __name__ == "__main__":
    _smoke()
