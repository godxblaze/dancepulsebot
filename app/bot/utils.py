from __future__ import annotations

import io

import segno
from aiogram import Bot

_bot_username: str | None = None


async def get_bot_username(bot: Bot) -> str:
    global _bot_username
    if _bot_username is None:
        _bot_username = (await bot.get_me()).username or ""
    return _bot_username


def qr_png(data: str) -> bytes:
    """Render `data` as a PNG QR code (raw bytes)."""
    buf = io.BytesIO()
    segno.make(data).save(buf, kind="png", scale=10)
    return buf.getvalue()


def bar(filled: int, total: int, width: int = 10) -> str:
    """Text progress bar from block characters, e.g. ███████░░░."""
    if total <= 0:
        return "░" * width
    n = max(0, min(width, round(width * filled / total)))
    return "█" * n + "░" * (width - n)
