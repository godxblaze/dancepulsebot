"""Заполняем чистую БД фейковыми данными для ручных тестов.

Конкретно:
- Делает developer'а (из .env) admin'ом.
- 4 группы с разным расписанием:
    Сальса ур.1 — Пн/Ср 19:00
    Бачата ур.2 — Вт/Чт 20:00 (намеренно перекошенная по полу — для recruit)
    Кизомба ур.3 — Сб 16:00
    Хастл ур.2 — Пт 19:30 (на паузе для теста pause-UI)
- В каждую — 20-30 фейковых Person'ов (tg_id в диапазоне 1_000_000-9_999_999,
  чтобы scripts.purge_fakes их потом снес).
- Один venue «Студия Ритм».
- В Сальсе делает несколько persons с support_level — будет видно в /pool.
- Расставляет несколько прошедших закрытых опросов с фейковыми ответами,
  чтобы /stats не был пустым.

Никаких реальных Telegram-сообщений не шлёт.
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta

from sqlalchemy import delete

from app.core.config import settings
from app.db.models import (
    Answer,
    Gender,
    Group,
    Person,
    Response,
    Survey,
    SurveyKind,
    SurveyStatus,
    Venue,
)
from app.db.session import init_db, session_scope
from app.services import admins as admins_svc
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import schools as schools_svc
from app.services import venues as venues_svc

# Pool of realistic Russian names
MALE_NAMES = [
    "Алексей", "Дмитрий", "Артём", "Иван", "Сергей", "Михаил", "Андрей", "Павел",
    "Никита", "Игорь", "Максим", "Роман", "Владислав", "Денис", "Юрий", "Тимофей",
    "Кирилл", "Антон", "Виктор", "Олег", "Эдуард", "Глеб", "Степан", "Илья",
]
FEMALE_NAMES = [
    "Анна", "Мария", "Екатерина", "Ольга", "Ирина", "Татьяна", "Дарья", "Алёна",
    "Ксения", "Юлия", "Анастасия", "Полина", "Виктория", "Светлана", "Елена",
    "Наталья", "Александра", "Маргарита", "Софья", "Ева", "Алина", "Кристина",
    "Лиза", "Вероника", "Карина",
]
SURNAMES = [
    "Иванов", "Петров", "Сидоров", "Морозов", "Кузнецов", "Васильев", "Соколов",
    "Павлов", "Романов", "Титов", "Лебедев", "Орлов", "Гусев", "Беляев", "Котов",
    "Семёнов", "Жуков", "Зайцев", "Громов", "Ушаков", "Михайлов", "Дмитриев",
    "Виноградов", "Богданов", "Воробьёв", "Фёдоров",
]


def _surname(male: bool) -> str:
    s = random.choice(SURNAMES)
    return s if male else (s + "а" if not s.endswith("ий") else s[:-2] + "ая")


def _gen_persons(n_male: int, n_female: int, next_tg_id: int) -> list[dict]:
    """Генератор фейковых person-словарей с уникальными tg_id."""
    result: list[dict] = []
    for _ in range(n_male):
        result.append({
            "tg_user_id": next_tg_id,
            "tg_username": f"user{next_tg_id}",
            "full_name": f"{random.choice(MALE_NAMES)} {_surname(True)}",
            "gender": Gender.male,
        })
        next_tg_id += 1
    for _ in range(n_female):
        result.append({
            "tg_user_id": next_tg_id,
            "tg_username": f"user{next_tg_id}",
            "full_name": f"{random.choice(FEMALE_NAMES)} {_surname(False)}",
            "gender": Gender.female,
        })
        next_tg_id += 1
    random.shuffle(result)
    return result


async def main() -> None:
    random.seed(42)
    await init_db()
    if not settings.developer_ids:
        raise SystemExit("DEVELOPER_TG_IDS не задан в .env")
    dev_id = settings.developer_ids[0]

    async with session_scope() as s:
        # 0) Чистим всё кроме admins (тебя сохраним)
        await s.execute(delete(Group))
        await s.execute(delete(Person))
        await s.execute(delete(Venue))
        await s.flush()

        # 1) Admin = developer (+ его owned-школа авто-создаётся)
        admin = await admins_svc.get_or_create(
            s, dev_id, None, "Тестовый Препод"
        )
        admin_id = admin.id
        school = await schools_svc.get_or_create_owned_school(s, admin_id)
        school_id = school.id

        # 2) Venue
        venue = await venues_svc.create_venue(
            s, school_id, "Студия Ритм", "Тверская 10"
        )
        venue_id = venue.id

        # 3) Группы
        # Сальса — баланс ОК
        g_salsa = await groups_svc.create_group(
            s, school_id, "Сальса · ур.1", "0,2", "19:00", 18,
            style="Сальса", level=1, venue_id=venue_id, room="зал 38",
            teachers_note="Маша + Петя",
        )
        # Бачата — перекос (мало парней, много девушек) → recruit нужен
        g_bachata = await groups_svc.create_group(
            s, school_id, "Бачата · ур.2", "1,3", "20:00", 18,
            style="Бачата", level=2, venue_id=venue_id, room="зал 22",
            teachers_note="Алина",
        )
        # Кизомба — баланс
        g_kizomba = await groups_svc.create_group(
            s, school_id, "Кизомба · ур.3", "5", "16:00", 18,
            style="Кизомба", level=3, venue_id=venue_id, room="зал 40",
        )
        # Хастл — на паузе (для теста UI)
        g_hustle = await groups_svc.create_group(
            s, school_id, "Хастл · ур.2", "4", "19:30", 18,
            style="Хастл", level=2, venue_id=venue_id, room="зал 19",
            teachers_note="Дима",
        )
        g_hustle.active = False
        await s.flush()

        # 4) Persons: tg_id из диапазона purge_fakes (1M-10M)
        next_tg = 1_000_001
        salsa_persons = _gen_persons(12, 13, next_tg)
        next_tg += len(salsa_persons)
        bachata_persons = _gen_persons(5, 20, next_tg)
        next_tg += len(bachata_persons)
        kizomba_persons = _gen_persons(10, 12, next_tg)
        next_tg += len(kizomba_persons)
        hustle_persons = _gen_persons(11, 9, next_tg)
        next_tg += len(hustle_persons)

        async def _add_batch(group: Group, batch: list[dict]) -> list[int]:
            ids: list[int] = []
            for spec in batch:
                person, _ = await persons_svc.add_to_group(
                    s, group, spec["tg_user_id"], spec["tg_username"], spec["full_name"]
                )
                await persons_svc.set_gender(s, person.id, spec["gender"])
                ids.append(person.id)
            return ids

        salsa_ids = await _add_batch(g_salsa, salsa_persons)
        bachata_ids = await _add_batch(g_bachata, bachata_persons)
        await _add_batch(g_kizomba, kizomba_persons)
        await _add_batch(g_hustle, hustle_persons)

        # 5) Несколько human'ов в личном пуле саппортов dev-препода (с уровнями) —
        #    из салсы парни. SupportPoolEntry: (admin, person) → level, per-teacher
        #    (Person.support_level снесён — раньше был один на всю школу).
        salsa_male_ids = [
            pid for pid, spec in zip(salsa_ids, salsa_persons, strict=True)
            if spec["gender"] == Gender.male
        ]
        for pid, lvl, helped in zip(
            salsa_male_ids[:5], [3, 3, 2, 2, 1], [12, 7, 4, 3, 1], strict=False,
        ):
            await persons_svc.set_pool_level(s, admin_id, pid, lvl)
            p = await s.get(Person, pid)
            p.helped_count = helped

        salsa_female_ids = [
            pid for pid, spec in zip(salsa_ids, salsa_persons, strict=True)
            if spec["gender"] == Gender.female
        ]
        for pid, lvl, helped in zip(
            salsa_female_ids[:4], [3, 2, 2, 1], [8, 5, 2, 1], strict=False,
        ):
            await persons_svc.set_pool_level(s, admin_id, pid, lvl)
            p = await s.get(Person, pid)
            p.helped_count = helped
        await s.flush()

        # 6) Прошедшие закрытые опросы (3 шт на сальсу, 2 на бачату) — чтобы /stats не был пустым
        now = datetime.now().replace(second=0, microsecond=0)
        for offset_days, group, member_ids in [
            (7, g_salsa, salsa_ids),
            (5, g_salsa, salsa_ids),
            (3, g_salsa, salsa_ids),
            (6, g_bachata, bachata_ids),
            (2, g_bachata, bachata_ids),
        ]:
            lesson_at = now - timedelta(days=offset_days)
            survey = Survey(
                group_id=group.id, kind=SurveyKind.attendance,
                lesson_at=lesson_at, status=SurveyStatus.closed,
            )
            s.add(survey)
            await s.flush()
            for pid in member_ids:
                roll = random.random()
                if roll < 0.65:
                    answer = Answer.yes
                    attended = random.random() < 0.85  # 85% обещавших реально пришли
                elif roll < 0.85:
                    answer = Answer.no
                    attended = False
                else:
                    answer = None
                    attended = None
                s.add(Response(
                    survey_id=survey.id, person_id=pid,
                    answer=answer,
                    actually_attended=attended,
                    answered_at=lesson_at - timedelta(hours=10) if answer else None,
                ))
            await s.flush()

        # 7) Тестовое будущее занятие, на которое БОТ САМ разошлёт опрос
        #    через scheduler — выставляем за пределами lead_hours изначально,
        #    но потом scheduler-tick переключится когда мы поставим время вручную.
        #    Для ручного теста удобнее ничего сейчас не делать; preпод сам
        #    может либо подождать ближайшего занятия по расписанию, либо
        #    запустить scripts.trigger_poll.

        group_names = [
            g_salsa.name, g_bachata.name, g_kizomba.name,
            g_hustle.name + " (на паузе)",
        ]
        print(f"✅ Admin: tg={dev_id} (id={admin_id})")
        print(f"✅ Группы: {group_names}")
        print(f"   Сальса: {len(salsa_ids)} чел.")
        print(f"   Бачата: {len(bachata_ids)} чел. (перекос: больше девушек)")
        print(f"   Кизомба: {len(kizomba_persons)} чел.")
        print(f"   Хастл: {len(hustle_persons)} чел. (на паузе)")
        print(f"✅ Venue: {venue.name}")
        print("✅ В пуле саппортов: 5 парней + 4 девушки c уровнями и helped_count")
        print("✅ 5 прошедших закрытых опросов с разной посещаемостью")


if __name__ == "__main__":
    asyncio.run(main())
