"""Snapshot/structure-тесты экранов препода (Phase 4 — Pulse redesign)."""

from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.bot.handlers.admin import _build_stats_text, _build_today_view, cb_group_card
from app.db.models import Answer, SurveyKind
from app.db.session import session_scope
from app.services import admins, groups, persons, surveys

_MSK = ZoneInfo("Europe/Moscow")


def _msk(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=_MSK).astimezone(UTC)


class _CbMessage:
    def __init__(self) -> None:
        self.text: str | None = None

    async def edit_text(self, text, **kw) -> None:
        self.text = text


class _Callback:
    def __init__(self, data: str, user_id: int) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = _CbMessage()

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        pass


async def _stats_text_with_n_closed(n: int) -> str:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 30, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18, style="Сальса")
        p, _ = await persons.add_to_group(s, g, 70, "u", "Ученик")
        for day in range(11, 11 + n):  # n распознаваемых дней мая
            survey, _ = await surveys.get_or_create_survey(
                s, g.id, SurveyKind.attendance, datetime(2026, 5, day, 19, 0)
            )
            await surveys.ensure_responses(s, survey.id, [p.id])
            await surveys.record_answer(s, survey.id, p.id, Answer.yes)
        await surveys.close_past_surveys(s, datetime(2026, 5, 28, 0, 0))
        teacher = await admins.get_by_tg_id(s, 30)
    return await _build_stats_text(teacher, "school")


async def test_today_block_anatomy(db, monkeypatch) -> None:
    """Блок /today ведёт заголовком 👯 <b>Title</b> · когда время — собран ui.title."""
    async with session_scope() as s:
        t = await admins.get_or_create(s, 20, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18, style="Хастл")
        await persons.add_to_group(s, g, 60, "u", "Ученик")
        tid = t.id
    monkeypatch.setattr(
        "app.bot.handlers.admin.utc_now", lambda: _msk(2026, 5, 25, 6, 0)
    )
    text, _markup = await _build_today_view(tid, "day")
    assert text.startswith("👯 <b>Хастл</b>")   # <emoji> <b>Title</b> анатомия
    assert "сегодня 19:00" in text


async def test_stats_header_plural_singular(db) -> None:
    """n=1 → «последнее 1 занятие» (корректная форма), заголовок 📊 <b>Title</b>."""
    text = await _stats_text_with_n_closed(1)
    assert text is not None
    assert "📊 <b>Сальса</b>" in text              # <emoji> <b>Title</b> анатомия
    assert "последнее 1 занятие:" in text


async def test_stats_header_plural_many(db) -> None:
    """n=5 → «последние 5 занятий» (many-форма)."""
    text = await _stats_text_with_n_closed(5)
    assert text is not None
    assert "последние 5 занятий:" in text


async def test_group_card_header_anatomy(db) -> None:
    """Карточка группы ведёт заголовком 📋 <b>Title</b> (раньше было «📋 «Title»»)."""
    async with session_scope() as s:
        t = await admins.get_or_create(s, 40, "t", "T")
        g = await groups.create_group(s, t.id, "Бачата", "0", "19:00", 18, style="Бачата")
        gid = g.id
    call = _Callback(f"grp:{gid}", 40)
    await cb_group_card(call)
    assert call.message.text is not None
    assert call.message.text.startswith("📋 <b>Бачата</b>")


async def test_group_card_student_count_plural(db) -> None:
    """Счётчик учеников — корректные русские формы для n∈{1,2,5} (не «Учеников: 1»)."""
    async def _card(tg_seed: int, n: int) -> str:
        async with session_scope() as s:
            t = await admins.get_or_create(s, tg_seed, "t", "T")
            g = await groups.create_group(s, t.id, "G", "0", "19:00", 18, style="X")
            for i in range(n):
                await persons.add_to_group(s, g, tg_seed * 100 + i, f"u{i}", f"Имя {i}")
            gid = g.id
        call = _Callback(f"grp:{gid}", tg_seed)
        await cb_group_card(call)
        return call.message.text

    assert "👥 1 ученик" in await _card(91, 1)
    assert "👥 2 ученика" in await _card(92, 2)
    assert "👥 5 учеников" in await _card(93, 5)
