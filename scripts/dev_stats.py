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
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select

from app.db.models import (
    Answer,
    Group,
    Response,
    School,
    SchoolMember,
    SupportCall,
    SupportCallStatus,
    Survey,
)
from app.db.session import init_db, session_scope
from app.services import groups as groups_svc
from app.services import surveys as surveys_svc
from app.services.schedule import format_lesson_at

WINDOW = timedelta(weeks=4)


async def _school_row(s, school: School, since: datetime) -> dict:
    n_admins = (await s.execute(
        select(func.count()).select_from(SchoolMember)
        .where(SchoolMember.school_id == school.id)
    )).scalar_one()
    n_groups = (await s.execute(
        select(func.count()).select_from(Group)
        .where(Group.school_id == school.id, Group.active.is_(True))
    )).scalar_one()
    last_activity = (await s.execute(
        select(func.max(Survey.created_at))
        .select_from(Survey)
        .join(Group, Survey.group_id == Group.id)
        .where(Group.school_id == school.id)
    )).scalar_one()
    total, yes, marked, attended = (await s.execute(
        select(
            func.count(Response.id),
            func.sum(case((Response.answer == Answer.yes, 1), else_=0)),
            func.sum(case((Response.actually_attended.is_not(None), 1), else_=0)),
            func.sum(case((Response.actually_attended.is_(True), 1), else_=0)),
        )
        .select_from(Response)
        .join(Survey, Response.survey_id == Survey.id)
        .join(Group, Survey.group_id == Group.id)
        .where(
            Group.school_id == school.id,
            Survey.lesson_at >= since,
            Response.answer.is_not(None),
        )
    )).one()
    stuck_calls = (await s.execute(
        select(func.count())
        .select_from(SupportCall)
        .join(Survey, SupportCall.survey_id == Survey.id)
        .join(Group, Survey.group_id == Group.id)
        .where(
            Group.school_id == school.id,
            Survey.lesson_at >= since,
            SupportCall.status.in_((SupportCallStatus.pending, SupportCallStatus.open)),
        )
    )).scalar_one()
    return {
        "school": school,
        "admins": n_admins,
        "groups": n_groups,
        "last_activity": last_activity,
        "responded": total or 0,
        "yes": yes or 0,
        "marked": marked or 0,
        "attended": attended or 0,
        "stuck_calls": stuck_calls,
    }


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
    since = datetime.now(UTC) - WINDOW
    query = sys.argv[1].lower() if len(sys.argv) > 1 else None

    async with session_scope() as s:
        schools = (await s.execute(select(School).order_by(School.id))).scalars().all()
        rows = [await _school_row(s, school, since) for school in schools]
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
