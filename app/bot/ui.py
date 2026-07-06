"""Pulse design system — единый визуальный язык бота.

Один источник правды для эмодзи-словаря, русской плюрализации, анатомии
карточки, прогресс-бара, гендерного баланса и ряда «назад». Все user-facing
экраны собираются через эти примитивы, чтобы визуал был консистентным, а
хэндлеры — тоньше.

Модуль НЕ форматирует время (это `app.services.schedule.format_lesson_at` со
school-tz) и НЕ зависит от `app.bot.utils` — наоборот, `utils.bar`
переэкспортируется отсюда, чтобы реализация бара была ровно одна.
"""

from __future__ import annotations

from collections.abc import Iterable
from html import escape
from typing import Protocol

from aiogram.types import InlineKeyboardButton

# ── Эмодзи-словарь (единый источник правды) ─────────────────────────────
# Бренд и разделы
BRAND = "💓"
CALENDAR = "📅"
PLACE = "📍"
TEACHER = "👤"
STUDENTS = "👥"
SCHOOL = "🏫"
STATS = "📊"
SUPPORT = "🆘"
SETTINGS = "⚙"
GROUP = "📋"   # карточка/список группы
PAIR = "👯"    # занятие в /today (пара танцоров)
# Статусы
YES = "✅"
NO = "❌"
PENDING = "⏳"
UNDELIVERED = "📵"  # опрос не дошёл (заблокировали бота / не нажали Start) — не «молчит»
BOX = "☐"
# Действия
EDIT = "✏️"
DELETE = "🗑"
PAUSE = "⏸"
RESUME = "▶️"
INVITE = "📨"
BROADCAST = "📢"
SEARCH = "🔍"
EXPORT = "📥"
ADD = "➕"
# Навигация
BACK = "◀"
NEXT = "▶"
SKIP = "⏭"
REMINDER = "⏰"
# Гендер
MALE = "🕺"
FEMALE = "💃"
# Прочее
EVENING = "🌙"
DAY = "☀️"
BALANCE = "⚖️"
TROPHY = "🏆"
SLEEP = "💤"
TIMEZONE = "🌐"

# Commitment-копирайт (B2): напоминания/пинги делают акцент на последствиях для
# пары — мягкий социальный стимул прийти, без штрафов/баллов.
COMMITMENT = "Без тебя пара останется без партнёра."


# ── Русская плюрализация ────────────────────────────────────────────────
def plural(n: int, one: str, few: str, many: str) -> str:
    """Выбор русской формы слова по числу `n`.

    one  — для 1, 21, 31, 101 … (n % 10 == 1, кроме 11)
    few  — для 2-4, 22-24 …      (n % 10 in 2..4, кроме 12-14)
    many — для 0, 5-20, 25-30 …

    Пример: ``plural(n, "ученик", "ученика", "учеников")``.
    """
    n = abs(n)
    if n % 100 in (11, 12, 13, 14):
        return many
    last = n % 10
    if last == 1:
        return one
    if last in (2, 3, 4):
        return few
    return many


def plural_with_count(n: int, one: str, few: str, many: str) -> str:
    """Как :func:`plural`, но с числом впереди: ``5 учеников``."""
    return f"{n} {plural(n, one, few, many)}"


# ── Прогресс-бар (единственная реализация в проекте) ────────────────────
def bar(filled: int, total: int, width: int = 10) -> str:
    """Текстовый прогресс-бар из блоков, напр. ███████░░░ (фикс. ширина)."""
    if total <= 0:
        return "░" * width
    n = max(0, min(width, round(width * filled / total)))
    if filled > 0 and n == 0:
        # round-half-to-even может дать 0 блоков при реальном ненулевом вкладе
        # (напр. filled=1, total=20, width=10 → round(0.5)==0) — бар не должен
        # выглядеть полностью пустым, если кто-то реально есть.
        n = 1
    return "█" * n + "░" * (width - n)


def balance_bars(male: int, female: int) -> str:
    """Двухстрочный блок гендерного баланса пар: 🕺-бар и 💃-бар одной шкалы."""
    total = male + female
    return (
        f"{MALE} {bar(male, total)} {male}\n"
        f"{FEMALE} {bar(female, total)} {female}"
    )


class _Roster(Protocol):
    male: list[str]
    female: list[str]
    unknown: list[str]
    total: int


def format_coming(roster: _Roster) -> str:
    """Блок «кто идёт» с разбивкой по полу (🕺/💃) — соц-доказательство для ученика
    и сводки. Имена экранируются (HTML parse mode). Пустая строка, если пока
    никто не идёт. Принимает любой объект с .male/.female/.unknown/.total."""
    if roster.total == 0:
        return ""
    verb = plural(roster.total, "Идёт", "Идут", "Идут")
    noun = plural(roster.total, "человек", "человека", "человек")
    lines = [f"{verb} {roster.total} {noun}:"]
    if roster.male:
        lines.append(f"{MALE} {len(roster.male)}: {escape(', '.join(roster.male))}")
    if roster.female:
        lines.append(f"{FEMALE} {len(roster.female)}: {escape(', '.join(roster.female))}")
    if roster.unknown:
        lines.append(f"• {len(roster.unknown)}: {escape(', '.join(roster.unknown))}")
    return "\n".join(lines)


# ── Анатомия карточки ───────────────────────────────────────────────────
def _as_lines(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [v for v in value if v is not None]


def card(
    header: str,
    meta: str | Iterable[str] | None = None,
    body: str | Iterable[str] | None = None,
    footer: str | Iterable[str] | None = None,
) -> str:
    """Собрать карточку в едином стиле Pulse.

    Структура (разделители — пустые строки, без ``<hr>``)::

        <header>            ← заголовок «<эмодзи> <b>Title</b>», caller форматирует
        <meta…>             ← мета-строки вплотную под заголовком (дата·время, препод)
                            ← пустая строка
        <body…>             ← тело / CTA
                            ← пустая строка
        <footer…>           ← тихий футер (📍 место и т.п.)

    Любая секция, кроме header, опциональна. Время приходит уже отформатированным
    (через ``format_lesson_at`` со school-tz) — карточка его не трогает.
    """
    lines = [header, *_as_lines(meta)]
    body_lines = _as_lines(body)
    if body_lines:
        lines.append("")
        lines.extend(body_lines)
    footer_lines = _as_lines(footer)
    if footer_lines:
        lines.append("")
        lines.extend(footer_lines)
    return "\n".join(lines)


def title(emoji: str, text: str) -> str:
    """Строка-заголовок карточки: ``<эмодзи> <b>text</b>`` (text не экранируется —
    caller экранирует пользовательский ввод сам, как и в остальном коде)."""
    return f"{emoji} <b>{text}</b>"


# ── Клавиатурные примитивы ──────────────────────────────────────────────
def back_row(callback_data: str, label: str | None = None) -> list[InlineKeyboardButton]:
    """Единый ряд «назад» (одна кнопка) для добавления в inline_keyboard.

    По умолчанию ``◀ Назад``; передай `label`, чтобы уточнить назначение
    (напр. ``◀ К списку``). Глиф ◀ — единый для всей «назад»-навигации.
    """
    return [InlineKeyboardButton(text=label or f"{BACK} Назад", callback_data=callback_data)]
