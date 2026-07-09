"""Phase 9: минимальные in-process live-gauges (db session duration, callback
latency) — не персистятся, не переживают рестарт процесса и не видны
отдельным процессам (scripts/dev_stats.py, scripts/stats_dashboard.py), только
живому боту (/devstats). Полноценный persist/tracing-фреймворк избыточен для
пилотного масштаба — это единственная причина, почему это не таблица в БД."""
from __future__ import annotations


class RollingStats:
    __slots__ = ("count", "total_seconds", "max_seconds")

    def __init__(self) -> None:
        self.count = 0
        self.total_seconds = 0.0
        self.max_seconds = 0.0

    def record(self, seconds: float) -> None:
        self.count += 1
        self.total_seconds += seconds
        self.max_seconds = max(self.max_seconds, seconds)

    def snapshot(self) -> dict:
        if self.count == 0:
            return {"count": 0, "avg_seconds": 0.0, "max_seconds": 0.0}
        return {
            "count": self.count,
            "avg_seconds": self.total_seconds / self.count,
            "max_seconds": self.max_seconds,
        }


class Counter:
    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = 0

    def inc(self) -> None:
        self.value += 1


class LastValue:
    """Последнее известное значение (не rolling avg) — для метрик вроде
    «длительность последнего тика», где важен самый свежий факт."""

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value: dict | None = None

    def set(self, value: dict) -> None:
        self.value = value


# Живёт здесь (не в scheduler.py/devstats.py) — оба модуля пишут/читают этот
# единственный инстанс, а scheduler.py уже импортирует devstats.py (Phase 9,
# outbox-health check), так что обратный импорт devstats.py → scheduler.py
# создал бы цикл.
last_tick = LastValue()
