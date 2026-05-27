"""Seed a demo teacher + group so you can test without the bot wizard.

Usage: python -m scripts.demo_seed

Pass --teacher-tg-id <id> to bind the demo teacher to your real Telegram id.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from app.core.logging import setup_logging
from app.db.session import init_db, session_scope
from app.services import admins as admins_svc
from app.services import groups as groups_svc
from app.services import schools as schools_svc

log = logging.getLogger("seed")


async def seed(teacher_tg_id: int) -> None:
    await init_db()
    async with session_scope() as s:
        teacher = await admins_svc.get_or_create(
            s, teacher_tg_id, "demo_teacher", "Демо Преподаватель"
        )
        school = await schools_svc.get_or_create_owned_school(s, teacher.id)
        existing = await groups_svc.school_groups(s, school.id)
        if existing:
            log.warning("Teacher already has groups — skipping group creation")
            code = existing[0].join_code
            name = existing[0].name
        else:
            group = await groups_svc.create_group(
                s, school.id, "Hip-Hop начинающие", "0,2", "19:00", 18
            )
            code = group.join_code
            name = group.name

    print("=" * 60)
    print(f"Teacher tg_user_id: {teacher_tg_id}")
    print(f"Group: {name}")
    print(f"Join payload: join_{code}")
    print(f"Join link: https://t.me/<bot_username>?start=join_{code}")
    print("=" * 60)


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--teacher-tg-id", type=int, default=100000001)
    args = parser.parse_args()
    asyncio.run(seed(args.teacher_tg_id))


if __name__ == "__main__":
    main()
