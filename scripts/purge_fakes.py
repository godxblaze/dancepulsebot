"""Снести фейковых учеников из dev-БД перед боевым запуском.

Идентификатор фейка — диапазон tg_user_id, который dev-сидер использует
(1_000_000 .. 9_999_999). Реальные Telegram user_id выше этого диапазона
на порядки (миллиарды).

Запуск (с остановленным ботом):
    python -m scripts.purge_fakes [--dry-run]
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import delete, select

from app.db.models import Person
from app.db.session import init_db, session_scope

FAKE_TG_MIN = 1_000_000
FAKE_TG_MAX = 9_999_999


async def main(dry_run: bool) -> None:
    await init_db()
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(Person.id, Person.full_name, Person.tg_user_id).where(
                    Person.tg_user_id.between(FAKE_TG_MIN, FAKE_TG_MAX)
                )
            )
        ).all()
        if not rows:
            print("Фейковых персон не найдено.")
            return
        print(f"Найдено {len(rows)} фейк-персон:")
        for pid, name, tg in rows:
            print(f"  - id={pid:3} tg={tg}  {name}")
        if dry_run:
            print("\n--dry-run: ничего не удаляю.")
            return
        await s.execute(
            delete(Person).where(
                Person.tg_user_id.between(FAKE_TG_MIN, FAKE_TG_MAX)
            )
        )
        print(f"\nУдалено {len(rows)} фейк-персон (Membership/Response/SupportClaim — каскадом).")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    asyncio.run(main(dry))
