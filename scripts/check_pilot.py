"""Пред-пилотный префлайт: читает базу и сообщает о состояниях, которые тихо
ломают опросы или автодобор на реальной школе. Сам не меняет бизнес-данные —
но вызывает init_db(), который может докатить схемные lightweight-миграции
(ALTER TABLE ADD COLUMN) и одноразовые бэкфиллы, если база отстала от кода.

Запуск:
    python -m scripts.check_pilot

Что проверяет:
  - люди БЕЗ пола в чьём-либо пуле саппортов (невидимы для добора нужного пола);
  - люди БЕЗ пола, ответившие «приду» на открытый опрос (врёт баланс-бар);
  - активные группы без дней недели (next_lesson_at = None → опрос не уйдёт);
  - активные группы без куратора добора (recruit lead) — добор уйдёт в fallback;
  - школы с невалидным IANA-timezone (даты занятий поедут).
"""
from __future__ import annotations

import asyncio
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.models import (
    Answer,
    Group,
    Person,
    Response,
    School,
    SupportPoolEntry,
    Survey,
    SurveyStatus,
)
from app.db.session import init_db, session_scope


async def run() -> None:
    await init_db()
    issues = 0

    async with session_scope() as s:
        # 1. NULL-gender в пулах саппортов
        rows = (
            await s.execute(
                select(Person.full_name, Person.id)
                .join(SupportPoolEntry, SupportPoolEntry.person_id == Person.id)
                .where(Person.gender.is_(None))
                .distinct()
            )
        ).all()
        if rows:
            issues += len(rows)
            print(f"⚠️  {len(rows)} чел. в пуле(ах) саппортов БЕЗ пола "
                  "(не попадут в добор нужного пола):")
            for name, pid in rows:
                print(f"     • {name} (person id={pid})")

        # 2. NULL-gender, ответившие «приду» на открытый опрос
        rows = (
            await s.execute(
                select(Person.full_name, Person.id, Group.name)
                .join(Response, Response.person_id == Person.id)
                .join(Survey, Survey.id == Response.survey_id)
                .join(Group, Group.id == Survey.group_id)
                .where(
                    Response.answer == Answer.yes,
                    Person.gender.is_(None),
                    Survey.status == SurveyStatus.open,
                )
                .distinct()
            )
        ).all()
        if rows:
            issues += len(rows)
            print(f"⚠️  {len(rows)} чел. ответили «приду» БЕЗ пола на открытый "
                  "опрос (баланс-бар добора может врать):")
            for name, pid, gname in rows:
                print(f"     • {name} (id={pid}) — «{gname}»")

        # 3. активные группы без дней недели
        rows = (
            await s.execute(
                select(Group.name, Group.id).where(
                    Group.active.is_(True),
                    (Group.weekdays.is_(None)) | (Group.weekdays == ""),
                )
            )
        ).all()
        if rows:
            issues += len(rows)
            print(f"⚠️  {len(rows)} активн. групп(ы) без дней недели "
                  "(опрос никогда не уйдёт):")
            for gname, gid in rows:
                print(f"     • «{gname}» (group id={gid})")

        # 4. активные группы без куратора добора
        rows = (
            await s.execute(
                select(Group.name, Group.id).where(
                    Group.active.is_(True),
                    Group.recruit_lead_admin_id.is_(None),
                )
            )
        ).all()
        if rows:
            print(f"ℹ️  {len(rows)} активн. групп(ы) без явного куратора добора "
                  "(добор уйдёт владельцу школы — назначь lead'а в карточке):")
            for gname, gid in rows:
                print(f"     • «{gname}» (group id={gid})")

        # 5. школы с невалидным timezone
        rows = (await s.execute(select(School.name, School.id, School.timezone))).all()
        for sname, sid, tz in rows:
            try:
                ZoneInfo(tz)
            except Exception:  # noqa: BLE001
                issues += 1
                print(f"⚠️  школа «{sname}» (id={sid}): невалидный timezone "
                      f"«{tz}» — даты занятий поедут.")

    if issues == 0:
        print("✅ Префлайт чист — критичных состояний для пилота не найдено.")
    else:
        print(f"\nИтого: {issues} проблем(ы) к разбору перед пилотом.")


if __name__ == "__main__":
    asyncio.run(run())
