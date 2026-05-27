"""Schedule math — no DB, easy to unit-test.

**Timezone convention** во всём проекте:
- `local_now()`, `Survey.lesson_at`, `LessonCancellation.lesson_at` — **naive local**
  (datetime в timezone школы, без tzinfo). lesson_at — display-friendly, без
  пересчёта при отображении пользователю.
- `*.created_at` колонки с `server_default=func.now()` — **naive UTC**
  (SQLite пишет UTC текстом без tz). Сравнения с `datetime.now(UTC).replace(tzinfo=None)`.

Смешивать local-naive и UTC-naive нельзя; конвертация — через `astimezone()`
на aware-datetime. См. `_notify_stale_support_calls` в [../scheduler/scheduler.py].
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.config import settings


def local_now() -> datetime:
    """Current naive datetime in the school's configured timezone."""
    return datetime.now(ZoneInfo(settings.timezone)).replace(tzinfo=None)


def parse_weekdays(weekdays: str) -> set[int]:
    """'0,2' -> {0, 2}. 0 = Monday."""
    return {int(p) for p in weekdays.split(",") if p.strip() != ""}


def format_weekdays(weekdays: str) -> str:
    names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    days = sorted(parse_weekdays(weekdays))
    return ", ".join(names[d] for d in days) if days else "—"


def next_lesson_at(weekdays: str, lesson_time: str, now: datetime) -> datetime | None:
    """Next lesson datetime strictly after `now`, or None if weekdays is empty.

    `now` and the result are naive datetimes in the school's local timezone.
    """
    days = parse_weekdays(weekdays)
    if not days:
        return None
    hour, minute = (int(x) for x in lesson_time.split(":"))
    for offset in range(0, 8):
        day = (now + timedelta(days=offset)).date()
        if day.weekday() not in days:
            continue
        candidate = datetime(day.year, day.month, day.day, hour, minute)
        if candidate > now:
            return candidate
    return None


def upcoming_lessons(
    weekdays: str, lesson_time: str, now: datetime, n: int = 5, horizon_days: int = 60
) -> list[datetime]:
    """До `n` ближайших дат занятий, начиная со следующего после `now`. Пустой
    список — если weekdays пустой или дни не попадают в горизонт."""
    days = parse_weekdays(weekdays)
    if not days:
        return []
    hour, minute = (int(x) for x in lesson_time.split(":"))
    result: list[datetime] = []
    for offset in range(0, horizon_days + 1):
        day = (now + timedelta(days=offset)).date()
        if day.weekday() not in days:
            continue
        candidate = datetime(day.year, day.month, day.day, hour, minute)
        if candidate > now:
            result.append(candidate)
            if len(result) >= n:
                break
    return result
