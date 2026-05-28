from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.services.schedule import (
    evening_fix_at,
    format_weekdays,
    next_lesson_at,
    parse_weekdays,
)

_MSK = ZoneInfo("Europe/Moscow")


def _msk(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Aware UTC datetime от человекочитаемого МСК-времени.

    Все тесты `next_lesson_at` оперируют aware UTC, потому что после TZ-
    рефактора lesson_at в БД хранится как aware UTC. Местное время школы
    (lesson_time '19:00' + weekday) применяется внутри `next_lesson_at`
    к now-converted-to-school-TZ.
    """
    return datetime(year, month, day, hour, minute, tzinfo=_MSK).astimezone(UTC)


def test_parse_weekdays() -> None:
    assert parse_weekdays("0,2") == {0, 2}
    assert parse_weekdays("") == set()
    assert parse_weekdays("1, 3 ,5") == {1, 3, 5}


def test_format_weekdays() -> None:
    assert format_weekdays("0,2") == "Пн, Ср"
    assert format_weekdays("") == "—"
    assert format_weekdays("6") == "Вс"


def test_next_lesson_today_later() -> None:
    # Wednesday 2026-05-20 14:00 МСК, lesson Mon/Wed 19:00 МСК → today 19:00 МСК
    now = _msk(2026, 5, 20, 14, 0)
    assert next_lesson_at("0,2", "19:00", now) == _msk(2026, 5, 20, 19, 0)


def test_next_lesson_today_already_passed() -> None:
    # Wednesday 20:00 МСК, lesson today already over → next is Monday
    now = _msk(2026, 5, 20, 20, 0)
    assert next_lesson_at("0,2", "19:00", now) == _msk(2026, 5, 25, 19, 0)


def test_next_lesson_wraps_week() -> None:
    # Thursday, lesson only on Monday → next Monday
    now = _msk(2026, 5, 21, 10, 0)
    assert next_lesson_at("0", "19:00", now) == _msk(2026, 5, 25, 19, 0)


def test_next_lesson_no_weekdays() -> None:
    assert next_lesson_at("", "19:00", _msk(2026, 5, 21, 10, 0)) is None


def test_next_lesson_skips_exact_now() -> None:
    # A lesson exactly at `now` is not strictly after it → next week.
    now = _msk(2026, 5, 25, 19, 0)  # Monday 19:00 МСК
    assert next_lesson_at("0", "19:00", now) == _msk(2026, 6, 1, 19, 0)


def test_evening_fix_at_is_prev_day_local_time() -> None:
    # Понедельник 19:00 МСК → разбор накануне (вс) 20:00 МСК.
    lesson = _msk(2026, 5, 25, 19, 0)
    assert evening_fix_at(lesson, "20:00", tz="Europe/Moscow") == _msk(2026, 5, 24, 20, 0)


def test_evening_fix_at_morning_lesson_still_prev_day() -> None:
    # Утреннее занятие в 11:00 — разбор всё равно накануне вечером (за ~15 ч),
    # не за фиксированное число часов.
    lesson = _msk(2026, 5, 25, 11, 0)
    fix = evening_fix_at(lesson, "20:00", tz="Europe/Moscow")
    assert fix == _msk(2026, 5, 24, 20, 0)
    assert fix < lesson
