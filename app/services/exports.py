"""CSV-экспорт данных admin'а — чтобы школа могла забрать свои данные обратно.

Два набора:
- students.csv — все Person'ы admin'а с метаданными;
- attendance.csv — все Response'ы по группам admin'а (обещание × факт).
"""
from __future__ import annotations

import csv
import io

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, Membership, Person, Response, Survey


def _csv_bytes(rows: list[list[str]], header: list[str]) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    # BOM, чтобы Excel правильно открыл UTF-8 с русским
    return ("﻿" + buf.getvalue()).encode("utf-8")


async def students_csv(session: AsyncSession, admin_id: int) -> bytes:
    """Все ученики admin'а: их группы списком через ';'."""
    persons_stmt = (
        select(Person).where(Person.admin_id == admin_id).order_by(Person.full_name)
    )
    persons = list((await session.execute(persons_stmt)).scalars().all())
    # group names per person
    group_stmt = (
        select(Membership.person_id, Group.name)
        .join(Group, Group.id == Membership.group_id)
        .where(Group.admin_id == admin_id)
    )
    groups_map: dict[int, list[str]] = {}
    for pid, name in (await session.execute(group_stmt)).all():
        groups_map.setdefault(pid, []).append(name)
    rows: list[list[str]] = []
    for p in persons:
        rows.append([
            str(p.id),
            p.full_name,
            p.tg_username or "",
            str(p.tg_user_id),
            p.gender.value if p.gender else "",
            str(p.support_level) if p.support_level is not None else "",
            str(p.helped_count),
            "; ".join(sorted(groups_map.get(p.id, []))),
        ])
    header = ["id", "name", "tg_username", "tg_user_id", "gender",
              "support_level", "helped_count", "groups"]
    return _csv_bytes(rows, header)


async def attendance_csv(session: AsyncSession, admin_id: int) -> bytes:
    """Все Response'ы по всем группам admin'а."""
    stmt = (
        select(Group.name, Survey.lesson_at, Survey.status,
               Person.full_name, Response.answer, Response.actually_attended)
        .join(Survey, Survey.group_id == Group.id)
        .join(Response, Response.survey_id == Survey.id)
        .join(Person, Person.id == Response.person_id)
        .where(Group.admin_id == admin_id)
        .order_by(Group.name, Survey.lesson_at, Person.full_name)
    )
    rows: list[list[str]] = []
    for group_name, lesson_at, status, person_name, answer, attended in (
        await session.execute(stmt)
    ).all():
        rows.append([
            group_name,
            f"{lesson_at:%Y-%m-%d %H:%M}",
            status.value if hasattr(status, "value") else str(status),
            person_name,
            answer.value if answer else "",
            "" if attended is None else ("yes" if attended else "no"),
        ])
    header = ["group", "lesson_at", "status", "person", "promised", "attended"]
    return _csv_bytes(rows, header)
