"""Schedule math — no DB, easy to unit-test.

**Timezone convention** (после TZ-рефактора):

Все datetime в проекте — **aware UTC**. И в БД хранятся как aware UTC.
Single source of truth — UTC; конвертация в школьный TZ происходит только
при отображении пользователю через `format_lesson_at()`.

API:
- `utc_now()` — текущее время aware UTC. Используется везде где сравнивается
  с lesson_at/created_at.
- `local_now()` — текущее время aware в школьном TZ. Нужно только для
  weekday/lesson_time-арифметики (определить «какое сейчас число и день
  недели» с точки зрения школы).
- `next_lesson_at(weekdays, lesson_time, now_utc)` — следующая дата занятия
  как aware UTC. Внутри конвертит now в школьный TZ для подсчёта weekday,
  собирает candidate в школьном TZ, возвращает в UTC.
- `format_lesson_at(dt_utc, fmt)` — отобразить aware UTC datetime в школьном
  TZ. Использовать вместо прямого f"{dt:%H:%M}" в любом user-facing тексте.

`weekdays` (например "0,2" = Пн+Ср) и `lesson_time` ("19:00") всегда живут
в школьном TZ — это часть бизнес-смысла «занятие в Пн в 19:00 по местному».
Конвертация в UTC происходит на лету при сохранении/чтении lesson_at.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.config import settings


def utc_now() -> datetime:
    """Текущее время как aware UTC."""
    return datetime.now(UTC)


def local_now() -> datetime:
    """Текущее время aware в школьном TZ. Используется ТОЛЬКО для расчёта
    weekday/lesson_time-контекста (в каком сейчас местном дне и часе)."""
    return datetime.now(ZoneInfo(settings.timezone))


def school_tz() -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def format_lesson_at(dt: datetime, fmt: str = "%d.%m в %H:%M") -> str:
    """Aware UTC → строка в школьном TZ. Если dt naive (legacy данные до
    миграции) — трактуем как UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(school_tz()).strftime(fmt)


def parse_weekdays(weekdays: str) -> set[int]:
    """'0,2' -> {0, 2}. 0 = Monday."""
    return {int(p) for p in weekdays.split(",") if p.strip() != ""}


def format_weekdays(weekdays: str) -> str:
    names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    days = sorted(parse_weekdays(weekdays))
    return ", ".join(names[d] for d in days) if days else "—"


def next_lesson_at(
    weekdays: str, lesson_time: str, now_utc: datetime
) -> datetime | None:
    """Следующее занятие aware UTC, строго после `now_utc`. None если
    weekdays пуст. `weekdays`/`lesson_time` живут в школьном TZ."""
    days = parse_weekdays(weekdays)
    if not days:
        return None
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    tz = school_tz()
    now_local = now_utc.astimezone(tz)
    hour, minute = (int(x) for x in lesson_time.split(":"))
    for offset in range(0, 8):
        day = (now_local + timedelta(days=offset)).date()
        if day.weekday() not in days:
            continue
        candidate_local = datetime(
            day.year, day.month, day.day, hour, minute, tzinfo=tz,
        )
        candidate_utc = candidate_local.astimezone(UTC)
        if candidate_utc > now_utc:
            return candidate_utc
    return None


def upcoming_lessons(
    weekdays: str,
    lesson_time: str,
    now_utc: datetime,
    n: int = 5,
    horizon_days: int = 60,
) -> list[datetime]:
    """До `n` ближайших aware UTC дат после `now_utc`. Пустой список — если
    weekdays пуст или дни не попадают в горизонт."""
    days = parse_weekdays(weekdays)
    if not days:
        return []
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)
    tz = school_tz()
    now_local = now_utc.astimezone(tz)
    hour, minute = (int(x) for x in lesson_time.split(":"))
    result: list[datetime] = []
    for offset in range(0, horizon_days + 1):
        day = (now_local + timedelta(days=offset)).date()
        if day.weekday() not in days:
            continue
        candidate_local = datetime(
            day.year, day.month, day.day, hour, minute, tzinfo=tz,
        )
        candidate_utc = candidate_local.astimezone(UTC)
        if candidate_utc > now_utc:
            result.append(candidate_utc)
            if len(result) >= n:
                break
    return result
