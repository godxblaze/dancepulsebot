"""Unit-тесты дизайн-системы Pulse (app/bot/ui.py)."""

from app.bot import ui


def test_plural_russian_forms() -> None:
    one, few, many = "ученик", "ученика", "учеников"
    assert ui.plural(1, one, few, many) == "ученик"
    assert ui.plural(2, one, few, many) == "ученика"
    assert ui.plural(3, one, few, many) == "ученика"
    assert ui.plural(4, one, few, many) == "ученика"
    assert ui.plural(5, one, few, many) == "учеников"
    assert ui.plural(0, one, few, many) == "учеников"
    # 11-14 — всегда «many», несмотря на последнюю цифру
    assert ui.plural(11, one, few, many) == "учеников"
    assert ui.plural(12, one, few, many) == "учеников"
    assert ui.plural(14, one, few, many) == "учеников"
    # десятки и сотни
    assert ui.plural(21, one, few, many) == "ученик"
    assert ui.plural(22, one, few, many) == "ученика"
    assert ui.plural(25, one, few, many) == "учеников"
    assert ui.plural(101, one, few, many) == "ученик"
    assert ui.plural(111, one, few, many) == "учеников"


def test_plural_with_count() -> None:
    assert ui.plural_with_count(1, "ученик", "ученика", "учеников") == "1 ученик"
    assert ui.plural_with_count(2, "ученик", "ученика", "учеников") == "2 ученика"
    assert ui.plural_with_count(5, "ученик", "ученика", "учеников") == "5 учеников"


def test_bar_fixed_width_and_glyphs() -> None:
    assert ui.bar(0, 10) == "░" * 10
    assert ui.bar(10, 10) == "█" * 10
    assert ui.bar(5, 10) == "█████░░░░░"
    # фикс-ширина всегда соблюдается
    assert len(ui.bar(3, 7)) == 10
    assert len(ui.bar(3, 7, width=6)) == 6
    # деление на ноль не падает — пустой бар
    assert ui.bar(0, 0) == "░" * 10
    # только два глифа
    assert set(ui.bar(4, 9)) <= {"█", "░"}
    # round-half-to-even: round(10*1/20)==round(0.5)==0 — но filled=1 реален,
    # бар не должен выглядеть полностью пустым.
    assert ui.bar(1, 20).count("█") == 1


def test_balance_bars() -> None:
    out = ui.balance_bars(8, 2)
    lines = out.split("\n")
    assert len(lines) == 2
    assert lines[0].startswith(ui.MALE) and lines[0].endswith("8")
    assert lines[1].startswith(ui.FEMALE) and lines[1].endswith("2")
    # обе шкалы — одной длины (10)
    assert "█" in lines[0] and "░" in lines[0]


def test_card_structure() -> None:
    # только заголовок
    assert ui.card("HEAD") == "HEAD"
    # заголовок + мета вплотную, тело и футер через пустую строку
    out = ui.card(
        "HEAD",
        meta=["META1", "META2"],
        body="BODY",
        footer="FOOT",
    )
    assert out == "HEAD\nMETA1\nMETA2\n\nBODY\n\nFOOT"
    # None-секции пропускаются, None внутри meta-итерируемого отфильтровывается
    assert ui.card("H", meta=["M", None], body=None) == "H\nM"


def test_title() -> None:
    assert ui.title(ui.BRAND, "Балет") == "💓 <b>Балет</b>"


def test_back_row() -> None:
    row = ui.back_row("grplist")
    assert len(row) == 1
    assert row[0].text == "◀ Назад"
    assert row[0].callback_data == "grplist"
    # кастомный лейбл
    custom = ui.back_row("grp:5", label="◀ К группе")
    assert custom[0].text == "◀ К группе"


def test_emoji_vocabulary_present() -> None:
    # ключевые маркеры словаря определены (single source of truth)
    for name in (
        "BRAND", "CALENDAR", "PLACE", "TEACHER", "STUDENTS", "SCHOOL", "STATS",
        "SUPPORT", "SETTINGS", "YES", "NO", "PENDING", "BOX", "EDIT", "DELETE",
        "PAUSE", "RESUME", "INVITE", "BACK", "MALE", "FEMALE",
    ):
        assert isinstance(getattr(ui, name), str) and getattr(ui, name)
