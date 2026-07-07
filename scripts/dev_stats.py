"""Разработческий срез вовлечённости по школам — для ручного разбора, когда
Telegram-варианта (/devstats) мало (сортировка, drill-down по группам).

Запуск:
    python -m scripts.dev_stats                # таблица по всем школам
    python -m scripts.dev_stats <school-name>   # + разбивка по группам этой школы
                                                 # (подстрока, регистронезависимо)

Окно вовлечённости — последние 4 недели, как и в /devstats. Ничего не пишет
в базу."""
from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

from app.db.models import School
from app.db.session import init_db, session_scope
from app.services import devstats as devstats_svc
from app.services import groups as groups_svc
from app.services import surveys as surveys_svc
from app.services.schedule import format_lesson_at


def _pct(part: int, total: int) -> str:
    return f"{round(100 * part / total)}%" if total else "—"


def _print_table(rows: list[dict]) -> None:
    header = (
        f"{'школа':<24} {'admin':>5} {'групп':>5} {'посл.акт.':>11} "
        f"{'отклик':>7} {'посещ.':>7} {'зависш.':>8}"
    )
    print(header)
    print("-" * len(header))
    for r in sorted(
        rows,
        key=lambda r: (r["yes"] / r["responded"]) if r["responded"] else -1,
    ):
        last = f"{r['last_activity']:%d.%m.%Y}" if r["last_activity"] else "никогда"
        print(
            f"{r['school'].name[:24]:<24} {r['admins']:>5} {r['groups']:>5} {last:>11} "
            f"{_pct(r['yes'], r['responded']):>7} {_pct(r['attended'], r['marked']):>7} "
            f"{r['stuck_calls']:>8}"
        )


async def _print_group_drilldown(s, school: School) -> None:
    print(f"\n— группы школы «{school.name}» —")
    groups = await groups_svc.school_groups(s, school.id)
    for g in groups:
        closed = await surveys_svc.recent_closed_surveys(s, g.id, limit=5)
        if not closed:
            print(f"  {g.name}: нет завершённых опросов")
            continue
        parts = []
        for survey in reversed(closed):
            summary = await surveys_svc.summarize(s, survey.id)
            when = format_lesson_at(survey.lesson_at, "%d.%m")
            parts.append(f"{when}:{summary.yes}/{summary.total}")
        print(f"  {g.name} ({'active' if g.active else 'inactive'}): " + "  ".join(parts))


async def run() -> None:
    await init_db()
    since = datetime.now(UTC) - devstats_svc.WINDOW
    query = sys.argv[1].lower() if len(sys.argv) > 1 else None

    async with session_scope() as s:
        rows = await devstats_svc.all_school_rows(s, since)
        if not rows:
            print("Школ пока нет.")
            return
        _print_table(rows)

        if query:
            matches = [r["school"] for r in rows if query in r["school"].name.lower()]
            if not matches:
                print(f"\nШкола по запросу «{query}» не найдена.")
            for school in matches:
                await _print_group_drilldown(s, school)


if __name__ == "__main__":
    asyncio.run(run())
