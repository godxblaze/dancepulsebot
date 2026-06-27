from __future__ import annotations

import io

import segno
from aiogram import Bot

from app.bot.ui import bar  # re-export: единственная реализация бара живёт в ui
from app.db.models import Group, Venue

__all__ = [
    "bar",
    "derive_group_name",
    "format_group_title",
    "format_place",
    "get_bot_username",
    "qr_png",
]

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


def format_place(group: Group, venue: Venue | None) -> str | None:
    """One-line venue+room description for a lesson, or None if nothing is set."""
    parts: list[str] = []
    if venue is not None:
        if venue.address:
            parts.append(f"{venue.name} — {venue.address}")
        else:
            parts.append(venue.name)
    if group.room:
        parts.append(group.room)
    return ", ".join(parts) if parts else None


def format_group_title(group: Group) -> str:
    """Display title built from style + level. Falls back to the legacy `name`
    column for groups created before those fields existed."""
    parts: list[str] = []
    if group.style:
        parts.append(group.style)
    if group.level is not None:
        parts.append(f"ур.{group.level}")
    if parts:
        return " · ".join(parts)
    return group.name  # legacy fallback


def derive_group_name(style: str | None, level: int | None) -> str:
    """Slug used as Group.name — keeps the column non-null and useful for old views."""
    parts: list[str] = []
    if style:
        parts.append(style)
    if level is not None:
        parts.append(f"ур.{level}")
    return " · ".join(parts) if parts else "Группа"
