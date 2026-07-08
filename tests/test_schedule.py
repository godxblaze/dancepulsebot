from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.services.schedule import (
    evening_fix_at,
    format_lesson_at,
    format_weekday_long,
    format_weekdays,
    next_lesson_at,
    parse_weekdays,
    past_lessons_since,
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


def test_format_weekday_long() -> None:
    # 2026-05-20 — среда по МСК
    assert format_weekday_long(_msk(2026, 5, 20, 19, 0), tz="Europe/Moscow") == "среда"
    # 00:30 четверга по МСК (= 21:30 UTC среды) — день берётся в школьном TZ,
    # а не в UTC, иначе было бы «среда»
    assert (
        format_weekday_long(_msk(2026, 5, 21, 0, 30), tz="Europe/Moscow") == "четверг"
    )


def test_format_lesson_at_russian_weekday_abbrev() -> None:
    """%a должен давать русское сокращение (Вт), а не английское из C-локали
    (Tue). День недели берётся в школьном TZ: 00:30 четверга по МСК —
    это ещё среда по UTC."""
    # 2026-05-19 — вторник
    label = format_lesson_at(_msk(2026, 5, 19, 19, 0), "%d.%m (%a) в %H:%M", tz="Europe/Moscow")
    assert label == "19.05 (Вт) в 19:00"
    assert "Tue" not in label
    # 00:30 четверга по МСК (= 21:30 UTC среды) — сокращение по местному дню
    label = format_lesson_at(_msk(2026, 5, 21, 0, 30), "%d.%m (%a)", tz="Europe/Moscow")
    assert label == "21.05 (Чт)"


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


def test_past_lessons_since_finds_missed_occurrence() -> None:
    # Занятия Пн/Ср 19:00 МСК. Бот лежал с вс 12:00 до вт 12:00 — понедельничное
    # занятие целиком пришлось на даунтайм и должно найтись бэкфиллом.
    since = _msk(2026, 5, 24, 12, 0)  # воскресенье
    now = _msk(2026, 5, 26, 12, 0)  # вторник
    assert past_lessons_since("0,2", "19:00", now, since) == [_msk(2026, 5, 25, 19, 0)]


def test_past_lessons_since_excludes_boundaries() -> None:
    # Ровно на границах (since, now] занятие не входит.
    lesson = _msk(2026, 5, 25, 19, 0)
    assert past_lessons_since("0", "19:00", lesson, lesson) == []
    now = lesson - timedelta(seconds=1)
    since = lesson - timedelta(days=7)
    assert past_lessons_since("0", "19:00", now, since) == []


def test_past_lessons_since_no_weekdays() -> None:
    now = _msk(2026, 5, 26, 12, 0)
    since = now - timedelta(days=2)
    assert past_lessons_since("", "19:00", now, since) == []


def test_past_lessons_since_multiple_occurrences() -> None:
    # Окно захватывает оба занятия недели (Пн и Ср).
    since = _msk(2026, 5, 24, 0, 0)  # воскресенье
    now = _msk(2026, 5, 28, 0, 0)  # четверг
    assert past_lessons_since("0,2", "19:00", now, since) == [
        _msk(2026, 5, 25, 19, 0),
        _msk(2026, 5, 27, 19, 0),
    ]
