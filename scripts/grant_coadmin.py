"""Dev-инструмент: выдать co-admin доступ к группе.

Использование:
    python -m scripts.grant_coadmin <group_id> <tg_user_id> [full_name]

Если у админа с этим tg_user_id ещё нет записи в admins — создаст и
проставит ему набор admin-команд в боте. Идемпотентно.

Например: owner ведёт группу id=3, хочет добавить второго преподавателя
с tg_user_id=12345 — `python -m scripts.grant_coadmin 3 12345 "Маша Иванова"`.
"""
from __future__ import annotations

import asyncio
import sys

from app.bot.bot import build_bot
from app.bot.commands import grant_admin_commands
from app.db.models import Group
from app.db.session import init_db, session_scope
from app.services import admins as admins_svc
from app.services import group_access as access_svc


async def main(group_id: int, tg_user_id: int, full_name: str | None) -> None:
    await init_db()
    bot = build_bot()
    try:
        async with session_scope() as s:
            group = await s.get(Group, group_id)
            if group is None:
                print(f"group_id={group_id} не найдена", file=sys.stderr)
                sys.exit(1)
            admin = await admins_svc.get_by_tg_id(s, tg_user_id)
            created = False
            if admin is None:
                if not full_name:
                    print(
                        f"админа с tg_user_id={tg_user_id} нет — укажи имя 3-м аргументом",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                admin = await admins_svc.get_or_create(s, tg_user_id, None, full_name)
                created = True
            access = await access_svc.grant_access(s, group.id, admin.id)
            role = access.role.value
        if created:
            try:
                await grant_admin_commands(bot, tg_user_id)
            except Exception as e:  # noqa: BLE001
                print(f"warning: не удалось выставить admin-меню в TG: {e}", file=sys.stderr)
            try:
                await bot.send_message(
                    tg_user_id,
                    f"🛠 Тебя добавили админом школы. Группа «{group.name}» уже доступна — /groups.",
                )
            except Exception as e:  # noqa: BLE001
                print(f"warning: не смог отправить ему уведомление: {e}", file=sys.stderr)
        else:
            try:
                await bot.send_message(
                    tg_user_id,
                    f"🛠 Тебя добавили co-админом группы «{group.name}» — /groups.",
                )
            except Exception as e:  # noqa: BLE001
                print(f"warning: не смог отправить ему уведомление: {e}", file=sys.stderr)
        print(f"ok: admin tg={tg_user_id} получил роль {role} в группе «{group.name}»")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    gid = int(sys.argv[1])
    tg = int(sys.argv[2])
    name = sys.argv[3] if len(sys.argv) > 3 else None
    asyncio.run(main(gid, tg, name))
