from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message

from app.db.session import session_scope
from app.services import groups as groups_svc
from app.services import persons as persons_svc

log = logging.getLogger(__name__)
router = Router()

JOIN_PREFIX = "join_"


@router.message(CommandStart(deep_link=True))
async def join_group(message: Message, command: CommandObject) -> None:
    payload = (command.args or "").strip()
    tg = message.from_user
    if not tg:
        return
    if not payload.startswith(JOIN_PREFIX):
        await message.answer(
            "Ссылка-приглашение не распознана. Попроси преподавателя прислать новую."
        )
        return

    code = payload[len(JOIN_PREFIX):]
    full_name = tg.full_name or tg.username or f"id{tg.id}"

    async with session_scope() as s:
        group = await groups_svc.get_by_join_code(s, code)
        if group is None:
            await message.answer("Группа не найдена — возможно, ссылка устарела.")
            return
        _, joined = await persons_svc.add_to_group(
            s, group, tg.id, tg.username, full_name
        )
        group_name = group.name

    if joined:
        await message.answer(
            f"✅ Ты записан в группу «{group_name}».\n"
            f"Перед каждым занятием бот спросит, придёшь ли ты. Просто жми кнопку."
        )
    else:
        await message.answer(f"Ты уже в группе «{group_name}». Всё в порядке.")
