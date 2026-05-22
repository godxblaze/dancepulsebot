from datetime import datetime

from app.services.schedule import format_weekdays, next_lesson_at, parse_weekdays


def test_parse_weekdays() -> None:
    assert parse_weekdays("0,2") == {0, 2}
    assert parse_weekdays("") == set()
    assert parse_weekdays("1, 3 ,5") == {1, 3, 5}


def test_format_weekdays() -> None:
    assert format_weekdays("0,2") == "Пн, Ср"
    assert format_weekdays("") == "—"
    assert format_weekdays("6") == "Вс"


def test_next_lesson_today_later() -> None:
    # Wednesday 2026-05-20 14:00, lesson Mon/Wed 19:00 -> today 19:00
    now = datetime(2026, 5, 20, 14, 0)
    assert next_lesson_at("0,2", "19:00", now) == datetime(2026, 5, 20, 19, 0)


def test_next_lesson_today_already_passed() -> None:
    # Wednesday 20:00, lesson today already over -> next is Monday
    now = datetime(2026, 5, 20, 20, 0)
    assert next_lesson_at("0,2", "19:00", now) == datetime(2026, 5, 25, 19, 0)


def test_next_lesson_wraps_week() -> None:
    # Thursday, lesson only on Monday -> next Monday
    now = datetime(2026, 5, 21, 10, 0)
    assert next_lesson_at("0", "19:00", now) == datetime(2026, 5, 25, 19, 0)


def test_next_lesson_no_weekdays() -> None:
    assert next_lesson_at("", "19:00", datetime(2026, 5, 21, 10, 0)) is None
