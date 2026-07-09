"""Phase 5 (B1/B2/B3): соц-доказательство в карточке опроса, commitment-копирайт
в напоминаниях/пинге/подтверждении, дедлайн ответа из вечерней фиксации."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.db.models import Answer, Gender, SurveyKind
from app.db.session import session_scope
from app.scheduler import scheduler as sched
from app.services import admins as admins_svc
from app.services import groups, persons, surveys
from tests._outbox_helpers import drain_outbox

_MSK = ZoneInfo("Europe/Moscow")
LESSON = (
    (datetime.now(UTC).astimezone(_MSK) + timedelta(days=14))
    .replace(hour=19, minute=0, second=0, microsecond=0)
    .astimezone(UTC)
)
_BEFORE_FIX = LESSON - timedelta(hours=30)  # раньше вечернего разбора (lesson-23h)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self._mid = 1000

    async def send_message(self, chat_id, text, **kwargs):
        self._mid += 1
        self.sent.append((chat_id, text))
        return SimpleNamespace(chat_id=chat_id, text=text, message_id=self._mid)


class FakeCallback:
    def __init__(self, data: str, user_id: int) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = None
        self.answers: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)


async def _poll(bot, group, lesson_at, now, **kwargs) -> None:
    """_poll_group now only enqueues — drain through the real sender worker
    so bot.sent reflects delivery, same as before Phase 4."""
    await sched._poll_group(bot, group, lesson_at, now, **kwargs)
    await drain_outbox(bot)


async def _remind(bot, group, lesson_at, now, **kwargs) -> None:
    await sched._maybe_remind(bot, group, lesson_at, now, **kwargs)
    await drain_outbox(bot)


async def _group_two_members(name_a="Аня", name_b="Боря"):
    async with session_scope() as s:
        t = await admins_svc.get_or_create(s, 100, "t", "Препод")
        g = await groups.create_group(s, t.id, "Бачата", "0", "19:00", 18)
        a, _ = await persons.add_to_group(s, g, 200, "u", name_a)
        await persons.set_gender(s, a.id, Gender.female)
        b, _ = await persons.add_to_group(s, g, 201, "u", name_b)
        await persons.set_gender(s, b.id, Gender.male)
        return g.id


async def _get_group(gid):
    async with session_scope() as s:
        return await groups.get_group(s, gid)


# ── B1: соц-доказательство «кто идёт» в карточке опроса ──────────────


async def test_poll_card_omits_social_proof_when_nobody_coming(db) -> None:
    gid = await _group_two_members()
    group = await _get_group(gid)
    bot = FakeBot()
    await _poll(bot, group, LESSON, _BEFORE_FIX, tz="Europe/Moscow")
    assert bot.sent
    # никто ещё не ответил «да» → блока «Идёт/Идут» нет
    assert all("Идёт" not in t and "Идут" not in t for _, t in bot.sent)


async def test_poll_card_shows_social_proof_after_yes(db) -> None:
    gid = await _group_two_members()
    group = await _get_group(gid)
    await _poll(FakeBot(), group, LESSON, _BEFORE_FIX, tz="Europe/Moscow")
    # Аня отвечает «да», добавляем новенького
    async with session_scope() as s:
        survey = await surveys.find_attendance_survey(s, gid, LESSON)
        a = await persons.get_in_group(s, gid, 200)
        await surveys.record_answer(s, survey.id, a.id, Answer.yes)
        g = await groups.get_group(s, gid)
        await persons.add_to_group(s, g, 202, "u", "Вася")
    group = await _get_group(gid)
    bot = FakeBot()
    await _poll(bot, group, LESSON, _BEFORE_FIX, tz="Europe/Moscow")
    card = next(t for c, t in bot.sent if c == 202)  # карточка новенького
    assert ("Идёт" in card or "Идут" in card) and "Аня" in card


async def test_poll_card_escapes_coming_names(db) -> None:
    async with session_scope() as s:
        t = await admins_svc.get_or_create(s, 100, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        evil, _ = await persons.add_to_group(s, g, 200, "u", "<b>Хакер</b>")
        await persons.set_gender(s, evil.id, Gender.male)
        gid = g.id
    group = await _get_group(gid)
    await _poll(FakeBot(), group, LESSON, _BEFORE_FIX, tz="Europe/Moscow")
    async with session_scope() as s:
        survey = await surveys.find_attendance_survey(s, gid, LESSON)
        ev = await persons.get_in_group(s, gid, 200)
        await surveys.record_answer(s, survey.id, ev.id, Answer.yes)
        g = await groups.get_group(s, gid)
        await persons.add_to_group(s, g, 201, "u", "Новый")
    group = await _get_group(gid)
    bot = FakeBot()
    await _poll(bot, group, LESSON, _BEFORE_FIX, tz="Europe/Moscow")
    card = next(t for c, t in bot.sent if c == 201)
    assert "&lt;b&gt;Хакер&lt;/b&gt;" in card  # имя эскейпнуто
    assert "<b>Хакер</b>" not in card  # сырого HTML нет


# ── B3: дедлайн ответа из вечерней фиксации ──────────────────────────


async def test_poll_card_shows_deadline_from_evening_fix(db) -> None:
    gid = await _group_two_members()
    group = await _get_group(gid)
    bot = FakeBot()
    await _poll(
        bot, group, LESSON, _BEFORE_FIX, tz="Europe/Moscow", evening_time="20:00"
    )
    assert bot.sent
    assert all("до 20:00" in t for _, t in bot.sent)  # дедлайн = вечерняя фиксация


async def test_poll_card_no_deadline_when_fix_passed(db) -> None:
    gid = await _group_two_members()
    group = await _get_group(gid)
    bot = FakeBot()
    # за 2ч до занятия вечерняя фиксация уже прошла → дедлайн не показываем
    await _poll(
        bot, group, LESSON, LESSON - timedelta(hours=2),
        tz="Europe/Moscow", evening_time="20:00",
    )
    assert bot.sent
    assert all("ответь до" not in t for _, t in bot.sent)


# ── B2: commitment-копирайт ──────────────────────────────────────────


async def test_reminder_has_commitment_copy(db) -> None:
    gid = await _group_two_members()
    group = await _get_group(gid)
    # сначала опрос, чтобы появились Response с message_id (молчат)
    await _poll(FakeBot(), group, LESSON, _BEFORE_FIX, tz="Europe/Moscow")
    bot = FakeBot()
    # 4h-окно (1ч < until <= 4ч)
    await _remind(
        bot, group, LESSON, LESSON - timedelta(hours=2), tz="Europe/Moscow"
    )
    rem = [t for _, t in bot.sent]
    assert rem
    assert all("партнёр" in t.lower() for t in rem)  # commitment про пару/партнёра


async def test_ping_has_commitment_copy(db) -> None:
    from app.bot.handlers.admin import cb_ping_pending

    gid = await _group_two_members()
    async with session_scope() as s:
        survey, _ = await surveys.get_or_create_survey(
            s, gid, SurveyKind.attendance, LESSON
        )
        members = await persons.group_members(s, gid)
        await surveys.ensure_responses(s, survey.id, [p.id for p in members])
        sid = survey.id
    bot = FakeBot()
    await cb_ping_pending(FakeCallback(f"ping:{sid}", 100), bot)
    assert bot.sent
    assert all("партнёр" in t.lower() for _, t in bot.sent)


async def test_answer_confirmation_no_has_commitment(db) -> None:
    from app.bot.handlers.survey import on_attendance

    gid = await _group_two_members()
    async with session_scope() as s:
        survey, _ = await surveys.get_or_create_survey(
            s, gid, SurveyKind.attendance, LESSON
        )
        sid = survey.id

    class _Msg:
        def __init__(self):
            self.text = None

        async def edit_text(self, text, **kwargs):
            self.text = text

    cb = FakeCallback(f"att:{sid}:no", 200)
    cb.message = _Msg()
    await on_attendance(cb)
    assert cb.message.text and "партнёр" in cb.message.text.lower()
