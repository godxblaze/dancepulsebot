"""Snapshot/structure-тесты геро-карточек ученика (Phase 3 — Pulse redesign)."""

from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.bot import ui
from app.db.session import session_scope
from app.scheduler import scheduler as sched
from app.services import admins, groups, persons
from app.services.surveys import ComingRoster, SurveySummary

_MSK = ZoneInfo("Europe/Moscow")


def _msk(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=_MSK).astimezone(UTC)


LESSON = _msk(2026, 5, 25, 19, 0)


class _Bot:
    """Минимальный fake bot — пишет отправленные сообщения."""

    def __init__(self) -> None:
        self.sent: list[SimpleNamespace] = []
        self._mid = 1000

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self._mid += 1
        m = SimpleNamespace(
            chat_id=chat_id, text=text, reply_markup=reply_markup, message_id=self._mid
        )
        self.sent.append(m)
        return m


async def test_survey_card_structure(db, monkeypatch) -> None:
    """Опросная карточка: 💓-заголовок, 📅-мета, CTA «Придёшь?» через пустую
    строку — собрана единым ui.card()."""
    async with session_scope() as s:
        t = await admins.get_or_create(s, 111, "t", "Препод")
        g = await groups.create_group(s, t.id, "Сальса", "0", "19:00", 18)
        await persons.add_to_group(s, g, 200, "u", "Аня")

    monkeypatch.setattr(sched, "utc_now", lambda: _msk(2026, 5, 25, 9, 0))
    bot = _Bot()
    await sched.tick(bot)

    card = next(m.text for m in bot.sent if "Придёшь?" in m.text)
    assert card.startswith("💓 <b>")             # бренд + жирный тайтл
    assert "📅 <b>" in card and "в 19:00" in card  # дата·время-мета
    assert "\n\n<b>Придёшь?</b>" in card           # CTA отделён пустой строкой, жирный


async def test_survey_summary_shows_balance_with_names() -> None:
    """Итоговая сводка: заголовок через format_group_title, общий бар, баланс
    пар 🕺/💃 с именами (сигнал для добора) и статусы отдельным блоком."""
    group = SimpleNamespace(name="Сальса", style="Сальса", level=1, lesson_time="19:00")
    summary = SurveySummary(
        total=5, yes=3, no=1, pending=1, no_names=["Олег"], pending_names=["Дима"]
    )
    coming = ComingRoster(male=["Боря"], female=["Аня", "Маша"], unknown=[])
    text = sched._format_summary(group, LESSON, summary, coming=coming)
    assert "💓 <b>Сальса · ур.1</b> — итоги опроса" in text
    assert "придут <b>3/5</b>" in text and ui.BAR_FILLED in text
    assert "🕺" in text and "1 — Боря" in text
    assert "💃" in text and "2 — Аня, Маша" in text
    # статусы — отдельным блоком через пустую строку
    assert "\n\n❌ не придут: Олег" in text and "⏳ молчат: Дима" in text


async def test_survey_summary_single_female() -> None:
    """Одна идущая: баланс-строка 💃 с именем, 🕺-строка с нулём не теряется."""
    group = SimpleNamespace(name="X", style="X", level=None, lesson_time="19:00")
    summary = SurveySummary(
        total=1, yes=1, no=0, pending=0, no_names=[], pending_names=[]
    )
    coming = ComingRoster(male=[], female=["Аня"], unknown=[])
    text = sched._format_summary(group, LESSON, summary, coming=coming)
    assert "💃" in text and "1 — Аня" in text
    assert "🕺" in text  # нулевая шкала парней всё равно видна
