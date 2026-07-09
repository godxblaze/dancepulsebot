"""Генератор реалистичного объёма данных для load-тестов и EXPLAIN ANALYZE.

Использование:
    python -m scripts.seed_load_test --dry-run
    python -m scripts.seed_load_test --database-url postgresql+asyncpg://... [--reset]

Никогда не читает `DATABASE_URL`/`settings.database_url` — цель передаётся
явно через `--database-url`, чтобы случайно не насеять фейковых данных в
боевую/dev-БД (см. память `[[live-side-effects-smoke-test]]`). `--dry-run` не
требует `--database-url` и ничего не пишет — только печатает план.

Масштаб по умолчанию (см. Roadmap Phase 2): 10 школ, ~400 групп (300-500),
~4000 человек (3-5к), реалистичные членства/пулы саппортов/занятия — часть
групп специально собрана в одном 15-минутном слоте расписания (стресс для
scheduler tick/Phase 8 backpressure), остальные размазаны по неделе.

Требует, чтобы в целевой БД уже была схема (`Base.metadata.create_all` —
идемпотентно, создаёт недостающее; не заменяет `alembic upgrade head`, но
достаточно для одноразовой disposable seed-БД).

Переиспользуется Phase 8 (backpressure load-test) и Phase 12 (финальный
регрешн-load-test) — масштаб параметризуем через флаги, а не хардкожен.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.db.models import (
    Admin,
    Answer,
    Base,
    Gender,
    Group,
    Membership,
    Person,
    Response,
    School,
    SchoolMember,
    SchoolMemberRole,
    SupportCall,
    SupportCallStatus,
    SupportPoolEntry,
    Survey,
    SurveyKind,
    SurveyStatus,
    Venue,
)

_WEEKDAY_TIME_CLUSTER = ("0", "19:00")  # «все понедельники в 19:00» — стресс-слот
_LESSON_TIMES = ["10:00", "11:30", "14:00", "16:00", "18:00", "19:00", "20:00"]


@dataclass
class Scale:
    schools: int = 10
    groups: int = 400  # середина заявленного 300-500
    persons: int = 4000  # середина заявленного 3-5к
    surveys_per_group: int = 4
    cluster_fraction: float = 0.3  # доля групп в одном 15-минутном слоте
    support_pool_fraction: float = 0.2  # доля людей в пуле саппортов школы
    support_call_fraction: float = 0.15  # доля surveys с открытым добором
    seed: int = 42

    def describe(self) -> str:
        return (
            f"schools={self.schools} groups={self.groups} persons={self.persons} "
            f"surveys≈{self.groups * self.surveys_per_group} "
            f"responses≈{self.groups * self.surveys_per_group * 10} "
            f"(cluster_fraction={self.cluster_fraction}, "
            f"support_pool_fraction={self.support_pool_fraction}, "
            f"support_call_fraction={self.support_call_fraction})"
        )


async def seed(database_url: str, scale: Scale, *, reset: bool) -> dict[str, int]:
    rng = random.Random(scale.seed)
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            existing = (await conn.execute(School.__table__.select().limit(1))).first()
            if existing is not None:
                if not reset:
                    raise SystemExit(
                        "target DB already has data — pass --reset to truncate first "
                        "(refusing to silently pile up duplicate seed data)"
                    )
                tables = ", ".join(
                    f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables)
                )
                await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))

            counts = await _insert_all(conn, rng, scale)
            await _reset_sequences(conn)
        return counts
    finally:
        await engine.dispose()


async def _reset_sequences(conn) -> None:
    """Все ID вставлены явно (без опоры на SERIAL-дефолт) — без этого шага
    identity-последовательность каждой таблицы осталась бы на исходном
    значении, и первая же вставка через приложение (или дальнейший load-test)
    столкнулась бы с уже занятым PK. Таблицы без surrogate `id` (напр.
    `fsm_state`, Phase 6 — PK это `key`) пропускаем — сбрасывать нечего."""
    for table in Base.metadata.sorted_tables:
        if "id" not in table.columns:
            continue
        await conn.execute(text(
            f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
            f"COALESCE((SELECT MAX(id) FROM {table.name}), 1), "
            f"(SELECT MAX(id) FROM {table.name}) IS NOT NULL)"
        ))


async def _insert_all(conn, rng: random.Random, scale: Scale) -> dict[str, int]:
    now = datetime.now(UTC)
    counts: dict[str, int] = {}

    schools = [
        {"id": i + 1, "name": f"Школа №{i + 1}", "timezone": "Europe/Moscow"}
        for i in range(scale.schools)
    ]
    await conn.execute(School.__table__.insert(), schools)
    counts["schools"] = len(schools)

    admins = [
        {
            "id": i + 1,
            "tg_user_id": 900_000_000 + i,
            "tg_username": f"seed_owner_{i}",
            "full_name": f"Владелец школы {i + 1}",
            "support_code": uuid.uuid4().hex[:16],
            "has_completed_setup": True,
        }
        for i in range(scale.schools)
    ]
    await conn.execute(Admin.__table__.insert(), admins)
    counts["admins"] = len(admins)

    school_members = [
        {"school_id": i + 1, "admin_id": i + 1, "role": SchoolMemberRole.owner.value}
        for i in range(scale.schools)
    ]
    await conn.execute(SchoolMember.__table__.insert(), school_members)
    counts["school_members"] = len(school_members)

    venues = []
    venue_id = 1
    school_venue_ids: dict[int, list[int]] = {i + 1: [] for i in range(scale.schools)}
    for i in range(scale.schools):
        for _ in range(rng.randint(1, 3)):
            venues.append({
                "id": venue_id, "school_id": i + 1,
                "name": f"Зал {venue_id}", "address": f"ул. Тестовая, {venue_id}",
            })
            school_venue_ids[i + 1].append(venue_id)
            venue_id += 1
    await conn.execute(Venue.__table__.insert(), venues)
    counts["venues"] = len(venues)

    groups = []
    n_cluster = int(scale.groups * scale.cluster_fraction)
    for g in range(scale.groups):
        school_id = (g % scale.schools) + 1
        if g < n_cluster:
            weekday, lesson_time = _WEEKDAY_TIME_CLUSTER
        else:
            weekday = str(rng.randint(0, 6))
            lesson_time = rng.choice(_LESSON_TIMES)
        groups.append({
            "id": g + 1, "school_id": school_id,
            "name": f"Группа {g + 1}",
            "style": rng.choice(["Бачата", "Сальса", "Хастл", "Кизомба"]),
            "level": rng.randint(0, 4), "weekdays": weekday, "lesson_time": lesson_time,
            "duration_min": rng.choice([60, 90, 120]), "lead_hours": 18,
            "venue_id": rng.choice(school_venue_ids[school_id]),
            "join_code": uuid.uuid4().hex[:12],
            "recruit_lead_admin_id": school_id,  # owner admin.id == school_id по построению
            "active": True,
        })
    await conn.execute(Group.__table__.insert(), groups)
    counts["groups"] = len(groups)

    persons = []
    school_person_ids: dict[int, list[int]] = {i + 1: [] for i in range(scale.schools)}
    for p in range(scale.persons):
        school_id = (p % scale.schools) + 1
        pid = p + 1
        persons.append({
            "id": pid, "school_id": school_id,
            "tg_user_id": 800_000_000 + p,
            "tg_username": f"seed_person_{p}",
            "full_name": f"Студент {pid}",
            "gender": rng.choice([Gender.male.value, Gender.female.value, None]),
            "helped_count": 0,
        })
        school_person_ids[school_id].append(pid)
    await conn.execute(Person.__table__.insert(), persons)
    counts["persons"] = len(persons)

    memberships = []
    group_member_ids: dict[int, list[int]] = {}
    for grp in groups:
        pool = school_person_ids[grp["school_id"]]
        size = min(len(pool), rng.randint(6, 15))
        members = rng.sample(pool, size) if size else []
        group_member_ids[grp["id"]] = members
        memberships.extend({"person_id": pid, "group_id": grp["id"]} for pid in members)
    if memberships:
        await conn.execute(Membership.__table__.insert(), memberships)
    counts["memberships"] = len(memberships)

    support_pool = []
    for school_id in range(1, scale.schools + 1):
        pool = school_person_ids[school_id]
        size = int(len(pool) * scale.support_pool_fraction)
        for pid in rng.sample(pool, size) if size else []:
            support_pool.append(
                {"admin_id": school_id, "person_id": pid, "level": rng.randint(1, 3)}
            )
    if support_pool:
        await conn.execute(SupportPoolEntry.__table__.insert(), support_pool)
    counts["support_pool_entries"] = len(support_pool)

    surveys = []
    survey_id = 1
    survey_group_map = []
    for grp in groups:
        for w in range(scale.surveys_per_group):
            lesson_at = now - timedelta(days=7 * (w + 1))
            surveys.append({
                "id": survey_id, "group_id": grp["id"], "kind": SurveyKind.attendance.value,
                "lesson_at": lesson_at, "status": SurveyStatus.closed.value,
                "summary_sent": rng.random() < 0.9,
            })
            survey_group_map.append((survey_id, grp["id"]))
            survey_id += 1
    await conn.execute(Survey.__table__.insert(), surveys)
    counts["surveys"] = len(surveys)

    responses = []
    for sid, gid in survey_group_map:
        for pid in group_member_ids.get(gid, []):
            # Executemany требует одинаковый набор ключей во всех строках —
            # явный None вместо пропуска ключа для ещё-не-ответивших.
            if rng.random() < 0.85:  # часть без ответа — реалистичный pending-хвост
                answer = rng.choice([Answer.yes.value, Answer.no.value])
                attended = answer == Answer.yes.value and rng.random() < 0.9
                answered_at = now
            else:
                answer = None
                attended = None
                answered_at = None
            responses.append({
                "survey_id": sid, "person_id": pid, "answer": answer,
                "actually_attended": attended, "answered_at": answered_at,
            })
    if responses:
        await conn.execute(Response.__table__.insert(), responses)
    counts["responses"] = len(responses)

    support_calls = []
    for sid, _gid in survey_group_map:
        if rng.random() < scale.support_call_fraction:
            support_calls.append({
                "survey_id": sid, "gender": rng.choice([Gender.male.value, Gender.female.value]),
                "needed": rng.randint(1, 3),
                "status": rng.choice([SupportCallStatus.pending.value, SupportCallStatus.open.value,
                                       SupportCallStatus.closed.value]),
            })
    if support_calls:
        await conn.execute(SupportCall.__table__.insert(), support_calls)
    counts["support_calls"] = len(support_calls)

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--database-url", default=None, help="Целевая (disposable!) Postgres DSN")
    parser.add_argument("--dry-run", action="store_true", help="Только план, ничего не пишет")
    parser.add_argument(
        "--reset", action="store_true", help="TRUNCATE перед сидом, если в БД уже есть данные"
    )
    parser.add_argument("--schools", type=int, default=Scale.schools)
    parser.add_argument("--groups", type=int, default=Scale.groups)
    parser.add_argument("--persons", type=int, default=Scale.persons)
    parser.add_argument("--seed-value", type=int, default=Scale.seed, dest="seed_value")
    args = parser.parse_args()

    scale = Scale(
        schools=args.schools, groups=args.groups, persons=args.persons, seed=args.seed_value
    )

    if args.dry_run:
        print("DRY RUN — ничего не будет записано.")
        print(scale.describe())
        return

    if not args.database_url:
        print(
            "--database-url обязателен вне --dry-run (никогда не читаем DATABASE_URL "
            "из окружения — только явная disposable-цель).",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.database_url == settings.database_url:
        print(
            "--database-url совпадает с DATABASE_URL приложения — отказ. "
            "Этот скрипт только для одноразовой disposable БД.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(scale.describe())
    counts = asyncio.run(seed(args.database_url, scale, reset=args.reset))
    for table, n in counts.items():
        print(f"{table:<24} {n:>8}")


if __name__ == "__main__":
    main()
