"""Tests for teacher commands and the new-group wizard."""
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.bot.handlers.admin import (
    _owned_group,
    cb_group_card,
    cb_group_delete,
    cb_group_invite,
    cb_group_students,
    cb_groups_list,
    cmd_stats,
    cmd_today,
)
from app.bot.utils import derive_group_name
from app.db.models import Answer, SurveyKind
from app.db.session import session_scope
from app.services import admins, groups, persons, surveys

# ── fakes ────────────────────────────────────────────────────────────


class FakeMessage:
    def __init__(self, user_id: int, text: str | None = None) -> None:
        self.from_user = SimpleNamespace(
            id=user_id, full_name="Препод Тест", username="teacher"
        )
        self.text = text
        self.replies: list[str] = []
        self.photo_captions: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.replies.append(text)

    async def answer_photo(self, photo, caption: str | None = None, **kwargs) -> None:
        self.photo_captions.append(caption or "")


class FakeCbMessage:
    def __init__(self) -> None:
        self.text: str | None = None
        self.reply_markup = None
        self.photo_captions: list[str] = []

    async def edit_text(self, text: str, **kwargs) -> None:
        self.text = text
        self.reply_markup = kwargs.get("reply_markup")

    async def edit_reply_markup(self, **kwargs) -> None:
        self.reply_markup = kwargs.get("reply_markup")

    async def answer(self, text: str, **kwargs) -> None:
        self.text = text

    async def answer_photo(self, photo, caption: str | None = None, **kwargs) -> None:
        self.photo_captions.append(caption or "")


class FakeCallback:
    def __init__(self, data: str, user_id: int) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeCbMessage()
        self.answers: list[str] = []

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append(text)


class FakeFSM:
    """Minimal stand-in for aiogram's FSMContext."""

    def __init__(self) -> None:
        self.state = None
        self.data: dict = {}

    async def set_state(self, state) -> None:
        self.state = state

    async def get_data(self) -> dict:
        return dict(self.data)

    async def update_data(self, **kwargs) -> dict:
        self.data.update(kwargs)
        return dict(self.data)

    async def clear(self) -> None:
        self.state = None
        self.data = {}


_MSK = ZoneInfo("Europe/Moscow")


def _msk(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Aware UTC datetime от человекочитаемого МСК-времени."""
    return datetime(year, month, day, hour, minute, tzinfo=_MSK).astimezone(UTC)


@pytest.fixture
def freeze_today(monkeypatch):
    """Pin the clock used by /today. Naive datetime трактуется как МСК."""

    def _set(dt: datetime) -> None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_MSK).astimezone(UTC)
        monkeypatch.setattr("app.bot.handlers.admin.utc_now", lambda: dt)

    return _set


# ── /claim (self-serve онбординг препода) ────────────────────────────


async def test_claim_grants_teacher_with_correct_secret(db, monkeypatch) -> None:
    from app.bot.handlers.admin import cmd_claim
    from app.core.config import settings

    monkeypatch.setattr(settings, "owner_claim_secret", "S3CRET")
    msg = FakeMessage(user_id=555, text="/claim S3CRET")
    await cmd_claim(msg, SimpleNamespace(args="S3CRET"), SimpleNamespace())

    assert any("преподаватель" in r for r in msg.replies)
    async with session_scope() as s:
        assert await admins.get_by_tg_id(s, 555) is not None


async def test_claim_rejects_wrong_secret(db, monkeypatch) -> None:
    from app.bot.handlers.admin import cmd_claim
    from app.core.config import settings

    monkeypatch.setattr(settings, "owner_claim_secret", "S3CRET")
    msg = FakeMessage(user_id=556, text="/claim nope")
    await cmd_claim(msg, SimpleNamespace(args="nope"), SimpleNamespace())

    assert any("Неверный код" in r for r in msg.replies)
    async with session_scope() as s:
        assert await admins.get_by_tg_id(s, 556) is None


async def test_claim_disabled_when_no_secret(db, monkeypatch) -> None:
    from app.bot.handlers.admin import cmd_claim
    from app.core.config import settings

    monkeypatch.setattr(settings, "owner_claim_secret", "")
    msg = FakeMessage(user_id=557, text="/claim anything")
    await cmd_claim(msg, SimpleNamespace(args="anything"), SimpleNamespace())

    assert msg.replies == []  # фича выключена — молчим
    async with session_scope() as s:
        assert await admins.get_by_tg_id(s, 557) is None


# ── /status (DB-хелсчек разработчика) ────────────────────────────────


async def test_status_dev_only(db, monkeypatch) -> None:
    from app.bot.handlers.admin import cmd_status
    from app.core.config import settings

    monkeypatch.setattr(settings, "developer_id", 777)
    async with session_scope() as s:
        await admins.get_or_create(s, 100, "t", "T")  # школа — от autouse-фикстуры
    # не-разработчик — молчим
    msg = FakeMessage(user_id=100)
    await cmd_status(msg)
    assert msg.replies == []
    # разработчик — получает хелсчек с масштабом
    dev = FakeMessage(user_id=777)
    await cmd_status(dev)
    assert dev.replies and "Bot OK" in dev.replies[0]
    assert "школ:" in dev.replies[0]


# ── /today ───────────────────────────────────────────────────────────


async def test_today_no_teacher(db) -> None:
    """Non-teacher who is also not a registered student falls through to the
    student today screen, which nudges them to ask their teacher for a link."""
    msg = FakeMessage(user_id=1)
    await cmd_today(msg)
    assert msg.replies and "попроси у преподавателя" in msg.replies[0]


async def test_today_teacher_no_lessons(db) -> None:
    async with session_scope() as s:
        await admins.get_or_create(s, 1, "t", "T")
    msg = FakeMessage(user_id=1)
    await cmd_today(msg)
    assert msg.replies == ["На сегодня и завтра занятий нет."]


async def test_today_lesson_tomorrow_no_survey(db, freeze_today) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 10, "t", "T")
        await groups.create_group(s, t.id, "Сальса", "0", "19:00", 18)
    freeze_today(datetime(2026, 5, 24, 12, 0))  # Sunday -> lesson Monday (tomorrow)
    msg = FakeMessage(user_id=10)
    await cmd_today(msg)
    assert len(msg.replies) == 1
    assert "завтра" in msg.replies[0]
    assert "опрос ещё не разослан" in msg.replies[0]


async def test_today_lesson_today_with_survey(db, freeze_today) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 10, "t", "T")
        g = await groups.create_group(s, t.id, "Хип-хоп", "0", "19:00", 18)
        p, _ = await persons.add_to_group(s, g, 50, "u", "Ученик")
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, _msk(2026, 5, 25, 19, 0),
        )
        await surveys.ensure_responses(s, survey.id, [p.id])
        await surveys.record_answer(s, survey.id, p.id, Answer.yes)
    freeze_today(datetime(2026, 5, 25, 9, 0))  # Monday, lesson today
    msg = FakeMessage(user_id=10)
    await cmd_today(msg)
    assert "сегодня" in msg.replies[0]
    assert "придут <b>1/1</b>" in msg.replies[0]


# ── /stats ───────────────────────────────────────────────────────────


async def test_stats_no_groups(db) -> None:
    async with session_scope() as s:
        await admins.get_or_create(s, 1, "t", "T")
    msg = FakeMessage(user_id=1)
    await cmd_stats(msg)
    assert "нет групп" in msg.replies[0]


async def test_stats_non_teacher_silent(db) -> None:
    msg = FakeMessage(user_id=999)
    await cmd_stats(msg)
    assert msg.replies == []


async def test_stats_no_closed_surveys(db) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 10, "t", "T")
        await groups.create_group(s, t.id, "G", "0", "19:00", 18)
    msg = FakeMessage(user_id=10)
    await cmd_stats(msg)
    assert "пока нет завершённых опросов" in msg.replies[0]


async def test_stats_with_closed_surveys(db) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 10, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        p, _ = await persons.add_to_group(s, g, 50, "u", "Ученик")
        for day in (11, 18):
            survey, _ = await surveys.get_or_create_survey(
                s, g.id, SurveyKind.attendance, datetime(2026, 5, day, 19, 0)
            )
            await surveys.ensure_responses(s, survey.id, [p.id])
            await surveys.record_answer(s, survey.id, p.id, Answer.yes)
        await surveys.close_past_surveys(s, datetime(2026, 5, 20, 0, 0))
    msg = FakeMessage(user_id=10)
    await cmd_stats(msg)
    text = msg.replies[0]
    assert "последние 2 занятия" in text  # plural fix: 2 → «занятия», не «занятий»
    assert "средний отклик (обещали): <b>100%</b>" in text


# ── _owned_group authorization ───────────────────────────────────────


async def test_owned_group_owner(db) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 10, "t", "Owner")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        gid = g.id
    async with session_scope() as s:
        owned = await _owned_group(s, gid, 10)
        assert owned is not None and owned.id == gid


async def test_owned_group_rejects_other_teacher(db) -> None:
    async with session_scope() as s:
        owner = await admins.get_or_create(s, 10, "o", "Owner")
        await admins.get_or_create(s, 20, "x", "Stranger")
        g = await groups.create_group(s, owner.id, "G", "0", "19:00", 18)
        gid = g.id
    async with session_scope() as s:
        assert await _owned_group(s, gid, 20) is None  # not the owner


async def test_owned_group_unknown_group(db) -> None:
    async with session_scope() as s:
        await admins.get_or_create(s, 10, "t", "T")
    async with session_scope() as s:
        assert await _owned_group(s, 999, 10) is None


# Wizard-тесты на FSM-handlers были удалены при миграции на aiogram-dialog
# ([app/bot/dialogs/new_group.py](../app/bot/dialogs/new_group.py)). UI-фреймворк
# тестируется upstream'ом; нашу бизнес-логику покрывает test_services.py.


# ── group-card callbacks ─────────────────────────────────────────────


async def _make_group_with_members(owner_tg: int = 10, n: int = 2) -> int:
    async with session_scope() as s:
        t = await admins.get_or_create(s, owner_tg, "t", "Owner")
        g = await groups.create_group(
            s, t.id, "Сальса", "0,2", "19:00", 18, style="Сальса"
        )
        for i in range(n):
            await persons.add_to_group(s, g, 600 + i, f"u{i}", f"Ученик {i}")
        return g.id


async def test_cb_group_card_shows_student_count(db) -> None:
    gid = await _make_group_with_members(n=2)
    call = FakeCallback(f"grp:{gid}", 10)
    await cb_group_card(call)
    assert call.message.text is not None
    assert "2 ученика" in call.message.text  # plural: 👥 2 ученика


async def test_cb_group_card_rejects_non_owner(db) -> None:
    gid = await _make_group_with_members(owner_tg=10)
    call = FakeCallback(f"grp:{gid}", 999)  # not the owner
    await cb_group_card(call)
    assert call.message.text is None  # card not rendered
    assert any("не найдена" in a for a in call.answers)


async def test_cb_group_students_lists_names(db) -> None:
    gid = await _make_group_with_members(n=2)
    call = FakeCallback(f"grpstu:{gid}", 10)
    await cb_group_students(call)
    # Names now live in the inline-keyboard buttons, not the message body.
    buttons = [
        btn.text
        for row in call.message.reply_markup.inline_keyboard
        for btn in row
    ]
    assert any("Ученик 0" in t for t in buttons)
    assert any("Ученик 1" in t for t in buttons)


async def test_cb_group_students_empty(db) -> None:
    gid = await _make_group_with_members(n=0)
    call = FakeCallback(f"grpstu:{gid}", 10)
    await cb_group_students(call)
    assert "пока нет учеников" in call.message.text


async def test_cb_group_invite_sends_qr_with_link(db, monkeypatch) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, 10, "t", "Owner")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        gid, code = g.id, g.join_code

    async def fake_username(bot) -> str:
        return "dancepulsebot"

    monkeypatch.setattr("app.bot.handlers.admin.get_bot_username", fake_username)
    call = FakeCallback(f"grpinv:{gid}", 10)
    await cb_group_invite(call, None)
    assert call.message.photo_captions, "invite must be sent as a photo"
    caption = call.message.photo_captions[0]
    assert f"start=join_{code}" in caption
    assert "dancepulsebot" in caption


async def test_cb_group_delete_removes_group_and_students(db) -> None:
    gid = await _make_group_with_members(n=2)
    call = FakeCallback(f"grpdelok:{gid}", 10)
    await cb_group_delete(call)
    assert "удалена" in call.message.text
    async with session_scope() as s:
        assert await groups.get_group(s, gid) is None
        assert await persons.count_members(s, gid) == 0  # FK cascade


async def test_cb_group_delete_rejects_non_owner(db) -> None:
    gid = await _make_group_with_members(owner_tg=10)
    call = FakeCallback(f"grpdelok:{gid}", 999)
    await cb_group_delete(call)
    assert any("не найдена" in a for a in call.answers)
    async with session_scope() as s:
        assert await groups.get_group(s, gid) is not None  # not deleted


async def test_cb_groups_list_empty(db) -> None:
    call = FakeCallback("grplist", 555)
    await cb_groups_list(call)
    assert call.message.text is not None and "нет своих групп" in call.message.text


# ── edit existing group ──────────────────────────────────────────────
#
# EditGroup мастер переведён на aiogram-dialog
# ([app/bot/dialogs/edit_group.py](../app/bot/dialogs/edit_group.py)).
# Бизнес-логика правок покрывается на уровне services — ниже.


async def test_update_group_recalculates_name(db) -> None:
    """update_group(level=3) + ручной name=derive_group_name(...) — поведение
    которое раньше зашивал _apply_group_edit; теперь это делает dialog."""
    gid = await _make_group_with_members(n=0)
    async with session_scope() as s:
        g = await groups.get_group(s, gid)
        await groups.update_group(s, g, level=3)
        await groups.update_group(s, g, name=derive_group_name(g.style, g.level))
    async with session_scope() as s:
        g = await groups.get_group(s, gid)
        assert g.level == 3
        assert g.name == "Сальса · ур.3"


async def test_update_group_room_set_and_clear(db) -> None:
    gid = await _make_group_with_members(n=0)
    async with session_scope() as s:
        g = await groups.get_group(s, gid)
        await groups.update_group(s, g, room="зал 38")
    async with session_scope() as s:
        assert (await groups.get_group(s, gid)).room == "зал 38"
    async with session_scope() as s:
        g = await groups.get_group(s, gid)
        await groups.update_group(s, g, room=None)
    async with session_scope() as s:
        assert (await groups.get_group(s, gid)).room is None
