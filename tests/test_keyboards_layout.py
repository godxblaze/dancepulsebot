"""Layout/ergonomics-аудит inline-клавиатур (Phase 5 — мобильная эргономика).

Гарантии для телефона:
- callback_data каждой кнопки ≤ 64 байт (лимит Bot API);
- ряд с ≥2 кнопками не содержит подписей длиннее 16 символов (иначе кнопки
  сжимаются/криво переносятся на узком экране);
- recruit-config — аккуратная схема (заголовок-ряд + ряд вариантов), не рваная;
- навигационные back-ряды — единый глиф ◀.
"""

from types import SimpleNamespace

import app.bot.keyboards as kb
from app.db.models import Gender

MAX_LABEL_PAIR = 16
CB_LIMIT = 64


def _person(pid: int, name: str, gender=Gender.female, helped: int = 0):
    return SimpleNamespace(id=pid, full_name=name, gender=gender, helped_count=helped)


def _group(gid: int, name: str, style: str = "Сальса", active: bool = True):
    return SimpleNamespace(id=gid, name=name, style=style, active=active)


def _all_keyboards() -> dict:
    persons = [_person(i, f"Имя Фамилия {i}") for i in range(3)]
    long_persons = [_person(1, "Александра Константинопольская-Преждлинноимённая")]
    groups = [_group(i, f"Группа {i}") for i in range(3)]
    return {
        "weekdays": kb.weekdays_keyboard({0, 2}),
        "attendance": kb.attendance_keyboard(5),
        "flip_answer": kb.flip_answer_keyboard(5, "yes"),
        "recruit_config": kb.recruit_config_keyboard(2, "19:00", 3, "curated"),
        "support_pick": kb.support_pick_keyboard(
            7, long_persons, set(), {1: 3}, can_expand=True
        ),
        "pool": kb.pool_keyboard(persons, {0: 1, 1: 2}),
        "students": kb.students_keyboard(9, persons),
        "groups": kb.groups_keyboard(groups),
        "group_actions_owner": kb.group_actions_keyboard(
            9, True, True, has_upcoming=True, has_closed_surveys=True
        ),
        "group_actions_coadmin": kb.group_actions_keyboard(9, False, True),
        "today_ping": kb.today_ping_keyboard([("Сальса", 1, 3)], span="day"),
        "stats": kb.stats_keyboard("school"),
        "level_picker": kb.level_picker_keyboard(),
        "gender": kb.gender_keyboard(),
        "confirm_delete": kb.confirm_delete_keyboard(9),
    }


def test_callback_data_within_limit() -> None:
    for name, markup in _all_keyboards().items():
        if markup is None:
            continue
        for row in markup.inline_keyboard:
            for btn in row:
                if btn.callback_data is not None:
                    nbytes = len(btn.callback_data.encode("utf-8"))
                    assert nbytes <= CB_LIMIT, (
                        f"{name}: callback_data {nbytes}b > {CB_LIMIT}: "
                        f"{btn.callback_data!r}"
                    )


def test_no_wide_rows_with_long_labels() -> None:
    for name, markup in _all_keyboards().items():
        if markup is None:
            continue
        for ri, row in enumerate(markup.inline_keyboard):
            if len(row) >= 2:
                for btn in row:
                    assert len(btn.text) <= MAX_LABEL_PAIR, (
                        f"{name} row {ri}: «{btn.text}» ({len(btn.text)} симв.) "
                        f"в ряду из {len(row)} кнопок — слишком длинно для телефона"
                    )


def test_recruit_config_tidy_scheme() -> None:
    rows = kb.recruit_config_keyboard(2, "19:00", 3, "open_race").inline_keyboard
    # header, th, header, eve, header, topup, header, 4×mode, back
    assert len(rows) == 12
    for hi in (0, 2, 4, 6):
        assert len(rows[hi]) == 1 and rows[hi][0].callback_data == "noop"
    assert len(rows[1]) == 5 and len(rows[3]) == 5 and len(rows[5]) == 4
    mode_rows = rows[7:11]
    assert all(len(r) == 1 for r in mode_rows)
    assert all((r[0].callback_data or "").startswith("rcfgmode:") for r in mode_rows)
    marked = [r[0].text for r in mode_rows if r[0].text.startswith("✅")]
    assert len(marked) == 1 and "Открытая гонка" in marked[0]  # текущий = open_race
    assert rows[-1][0].text.startswith("◀")


def test_back_rows_use_arrow_glyph() -> None:
    kbs = _all_keyboards()
    for name in ("students", "group_actions_owner", "recruit_config"):
        last = kbs[name].inline_keyboard[-1]
        assert len(last) == 1 and last[0].text.startswith("◀"), name


def test_group_actions_long_labels_full_width() -> None:
    """Длинные действия («✏️ Отметить посещаемость») идут на всю ширину."""
    rows = kb.group_actions_keyboard(
        9, True, True, has_upcoming=True, has_closed_surveys=True
    ).inline_keyboard
    for row in rows:
        if len(row) == 1:
            continue
        assert all(len(b.text) <= MAX_LABEL_PAIR for b in row)
