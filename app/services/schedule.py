"""Schedule math — no DB, easy to unit-test.

**Timezone convention** (после TZ-рефактора + per-school timezone):

Все datetime в проекте — **aware UTC**. И в БД хранятся как aware UTC.
Single source of truth — UTC; конвертация в школьный TZ происходит только
при отображении пользователю через `format_lesson_at(dt, fmt, tz=...)`.

TZ каждой школы хранится в `School.timezone` (IANA name). Settings.timezone
оставлен как **fallback** для случаев когда school ещё не загружена (например
в tick'е до того как известна группа).

API:
- `utc_now()` — текущее время aware UTC.
- `school_tz_zone(tz_name)` — string → ZoneInfo с fallback на settings.timezone.
- `format_lesson_at(dt, fmt, tz)` — отобразить aware UTC в указанном TZ.
- `next_lesson_at(weekdays, lesson_time, now, tz)` — следующая дата с учётом
  weekday/lesson_time в указанном TZ; возвращает aware UTC.

`weekdays` ("0,2" = Пн+Ср) и `lesson_time` ("19:00") живут в школьном TZ —
это бизнес-смысл «занятие в Пн в 19:00 по местному».
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.config import settings


def utc_now() -> datetime:
    """Текущее время как aware UTC."""
    return datetime.now(UTC)


def as_utc(dt: datetime) -> datetime:
    """Привести к aware UTC. Naive (как читается из SQLite) трактуем как UTC —
    для сравнения lesson_at, прочитанного из БД, с `utc_now()`."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def school_tz_zone(tz_name: str | None = None) -> ZoneInfo:
    """`tz_name` → ZoneInfo. None или невалидный — fallback на
    settings.timezone (для обратной совместимости со старым кодом)."""
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001
            pass
    return ZoneInfo(settings.timezone)


def format_lesson_at(
    dt: datetime, fmt: str = "%d.%m в %H:%M", *, tz: str | None = None,
) -> str:
    """Aware UTC → строка в указанном TZ школы. Naive datetime трактуется
    как UTC (legacy fallback). tz=None → settings.timezone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(school_tz_zone(tz)).strftime(fmt)


def parse_weekdays(weekdays: str) -> set[int]:
    """'0,2' -> {0, 2}. 0 = Monday."""
    return {int(p) for p in weekdays.split(",") if p.strip() != ""}


def format_weekdays(weekdays: str) -> str:
    names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    days = sorted(parse_weekdays(weekdays))
    return ", ".join(names[d] for d in days) if days else "—"


def next_lesson_at(
    weekdays: str,
    lesson_time: str,
    now_utc: datetime,
    *,
    tz: str | None = None,
) -> datetime | None:
    """Следующее занятие aware UTC, строго после `now_utc`. None если
    weekdays пуст. `weekdays`/`lesson_time` живут в школьном TZ (`tz`)."""
    days = parse_weekdays(weekdays)
    if not days:
        return None
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    school_tz = school_tz_zone(tz)
    now_local = now_utc.astimezone(school_tz)
    hour, minute = (int(x) for x in lesson_time.split(":"))
    for offset in range(0, 8):
        day = (now_local + timedelta(days=offset)).date()
        if day.weekday() not in days:
            continue
        candidate_local = datetime(
            day.year, day.month, day.day, hour, minute, tzinfo=school_tz,
        )
        candidate_utc = candidate_local.astimezone(UTC)
        if candidate_utc > now_utc:
            return candidate_utc
    return None


def evening_fix_at(
    lesson_at: datetime,
    evening_time: str,
    *,
    tz: str | None = None,
) -> datetime:
    """Момент «вечернего разбора» накануне занятия: календарный день перед
    датой занятия (в школьном TZ) в `evening_time` ("HH:MM"). Возвращает aware
    UTC. Всегда строго раньше `lesson_at` (накануне вечером < занятия)."""
    if lesson_at.tzinfo is None:
        lesson_at = lesson_at.replace(tzinfo=UTC)
    school_tz = school_tz_zone(tz)
    lesson_local = lesson_at.astimezone(school_tz)
    prev_day = lesson_local.date() - timedelta(days=1)
    hour, minute = (int(x) for x in evening_time.split(":"))
    fix_local = datetime(
        prev_day.year, prev_day.month, prev_day.day, hour, minute, tzinfo=school_tz,
    )
    return fix_local.astimezone(UTC)


def upcoming_lessons(
    weekdays: str,
    lesson_time: str,
    now_utc: datetime,
    n: int = 5,
    horizon_days: int = 60,
    *,
    tz: str | None = None,
) -> list[datetime]:
    """До `n` ближайших aware UTC дат после `now_utc`. Пустой список — если
    weekdays пуст или дни не попадают в горизонт."""
    days = parse_weekdays(weekdays)
    if not days:
        return []
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    school_tz = school_tz_zone(tz)
    now_local = now_utc.astimezone(school_tz)
    hour, minute = (int(x) for x in lesson_time.split(":"))
    result: list[datetime] = []
    for offset in range(0, horizon_days + 1):
        day = (now_local + timedelta(days=offset)).date()
        if day.weekday() not in days:
            continue
        candidate_local = datetime(
            day.year, day.month, day.day, hour, minute, tzinfo=school_tz,
        )
        candidate_utc = candidate_local.astimezone(UTC)
        if candidate_utc > now_utc:
            result.append(candidate_utc)
            if len(result) >= n:
                break
    return result
