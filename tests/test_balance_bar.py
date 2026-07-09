"""Phase 7 (B8): живой баланс-бар — сохранение ref recruit-сообщения и его
обновление при claim/skip."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.bot.handlers.recruit import cb_support_join, cb_support_skip
from app.db.models import Answer, Gender, SupportCallStatus, SurveyKind
from app.db.session import session_scope
from app.scheduler import scheduler as sched
from app.services import admins as admins_svc
from app.services import groups, persons, supports, surveys
from tests._outbox_helpers import drain_outbox

_MSK = ZoneInfo("Europe/Moscow")
LESSON = (
    (datetime.now(UTC).astimezone(_MSK) + timedelta(days=14))
    .replace(hour=19, minute=0, second=0, microsecond=0)
    .astimezone(UTC)
)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.edits: list[dict] = []
        self._mid = 5000

    async def send_message(self, chat_id, text, **kwargs):
        self._mid += 1
        self.sent.append((chat_id, text))
        return SimpleNamespace(chat_id=chat_id, message_id=self._mid, text=text)

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        self.edits.append({"text": text, "chat_id": chat_id, "message_id": message_id})


class RaisingEditBot(FakeBot):
    async def edit_message_text(self, *a, **k):
        raise RuntimeError("message to edit not found")


class FakeCbMessage:
    def __init__(self) -> None:
        self.text = None
        self.reply_markup = None

    async def edit_text(self, text, **kwargs) -> None:
        self.text = text
        self.reply_markup = kwargs.get("reply_markup")


class FakeCallback:
    def __init__(self, data: str, user_id: int) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeCbMessage()
        self.answers: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)


async def _imbalanced_curated():
    """curated-дефицит (1 vs 4) с парнем-саппортом в пуле lead'а."""
    async with session_scope() as s:
        t = await admins_svc.get_or_create(s, 100, "t", "Препод")
        g = await groups.create_group(s, t.id, "Бачата", "0", "19:00", 18)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, LESSON
        )
        uid = 200
        for gen, count in ((Gender.male, 1), (Gender.female, 4)):
            for _ in range(count):
                p, _ = await persons.add_to_group(s, g, uid, "u", f"P{uid}")
                await persons.set_gender(s, p.id, gen)
                await surveys.record_answer(s, survey.id, p.id, Answer.yes)
                uid += 1
        other = await groups.create_group(s, t.id, "Пул", "2", "20:00", 18)
        sup, _ = await persons.add_to_group(s, other, 700, "u", "Саппорт")
        await persons.set_gender(s, sup.id, Gender.male)
        await persons.set_pool_level(s, t.id, sup.id, 2)
        return SimpleNamespace(group=g, survey_id=survey.id, lead_tg=100)


async def _open_call_with_ref(needed=2, set_ref=True):
    """Open-call с (опционально) сохранённым ref на сообщение lead'а (100, 555)
    и парнем-саппортом 700 в школе."""
    async with session_scope() as s:
        t = await admins_svc.get_or_create(s, 100, "t", "Препод")
        g = await groups.create_group(s, t.id, "Бачата", "0", "19:00", 18)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, LESSON
        )
        uid = 200
        for gen, count in ((Gender.male, 2), (Gender.female, 4)):  # дефицит 2 (male)
            for _ in range(count):
                p, _ = await persons.add_to_group(s, g, uid, "u", f"P{uid}")
                await persons.set_gender(s, p.id, gen)
                await surveys.record_answer(s, survey.id, p.id, Answer.yes)
                uid += 1
        call = await supports.create_call(
            s, survey.id, Gender.male, needed, SupportCallStatus.open
        )
        if set_ref:
            await supports.set_picker_ref(s, call.id, 100, 555)
        other = await groups.create_group(s, t.id, "Пул", "2", "20:00", 18)
        sup, _ = await persons.add_to_group(s, other, 700, "u", "Саппорт")
        await persons.set_gender(s, sup.id, Gender.male)
        return SimpleNamespace(call_id=call.id, survey_id=survey.id)


async def test_picker_send_saves_message_ref(db) -> None:
    ctx = await _imbalanced_curated()
    bot = FakeBot()
    await sched._maybe_recruit(bot, ctx.group, LESSON, LESSON - timedelta(hours=3))
    await drain_outbox(bot)
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, ctx.survey_id)
        assert call.picker_chat_id == ctx.lead_tg
        assert call.picker_message_id is not None


async def test_claim_updates_balance_message(db) -> None:
    ctx = await _open_call_with_ref(needed=2)
    bot = FakeBot()
    await cb_support_join(FakeCallback(f"sup:join:{ctx.call_id}", 700), bot)
    assert bot.edits, "balance message should be edited on claim"
    edit = bot.edits[-1]
    assert edit["chat_id"] == 100 and edit["message_id"] == 555
    assert "баланс пар" in edit["text"]
    assert "откликнулись 1/2" in edit["text"]


async def test_skip_rolls_back_balance_message(db) -> None:
    ctx = await _open_call_with_ref(needed=2)
    bot = FakeBot()
    await cb_support_join(FakeCallback(f"sup:join:{ctx.call_id}", 700), bot)
    await cb_support_skip(FakeCallback(f"sup:skip:{ctx.call_id}", 700), bot)
    edit = bot.edits[-1]
    assert edit["chat_id"] == 100 and edit["message_id"] == 555
    assert "откликнулись 0/2" in edit["text"]  # claim откатан


async def test_claim_without_ref_is_graceful(db) -> None:
    ctx = await _open_call_with_ref(needed=2, set_ref=False)
    bot = FakeBot()
    await cb_support_join(FakeCallback(f"sup:join:{ctx.call_id}", 700), bot)
    assert bot.edits == []  # нет ref → не редактируем
    async with session_scope() as s:
        assert await supports.count_claims(s, ctx.call_id) == 1  # claim записан


async def test_claim_edit_failure_is_graceful(db) -> None:
    ctx = await _open_call_with_ref(needed=2)
    bot = RaisingEditBot()
    # edit_message_text бросает (сообщение удалено) — хэндлер не падает
    await cb_support_join(FakeCallback(f"sup:join:{ctx.call_id}", 700), bot)
    async with session_scope() as s:
        assert await supports.count_claims(s, ctx.call_id) == 1
