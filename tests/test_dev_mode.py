"""Phase 10: dev/teacher UI-mode — non-developer denial on all 3 surfaces
(menu/dashboard/toggle), toggle persistence via dev_ui_pref (not FSM/session),
teacher-mode passthrough onboarding (identical to a real new teacher), and
the extended dashboard screen (Pulse formatting + HTML escaping)."""
from types import SimpleNamespace

from app.bot.dialogs.states import CreateSchoolSG
from app.bot.handlers.admin import cb_dev_dashboard, cb_dev_mode_toggle, cmd_devmode, cmd_start
from app.core.config import settings
from app.db.models import Admin, School
from app.db.session import session_scope
from app.services import admins, schools
from app.services import dev_ui as dev_ui_svc


class FakeMessage:
    def __init__(self, user_id: int, full_name: str = "Dev") -> None:
        self.from_user = SimpleNamespace(id=user_id, full_name=full_name, username="dev")
        self.bot = None
        self.replies: list[str] = []
        self.markups: list = []

    async def answer(self, text: str, **kwargs) -> None:
        self.replies.append(text)
        self.markups.append(kwargs.get("reply_markup"))

    async def edit_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)
        self.markups.append(kwargs.get("reply_markup"))


class FakeCallback:
    def __init__(self, data: str, user_id: int, message: FakeMessage) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id, full_name="Dev", username="dev")
        self.message = message
        self.answers: list[str] = []

    async def answer(self, text: str = "", **kwargs) -> None:
        self.answers.append(text)


class FakeDialogManager:
    """Записывает, с каким State стартовал dialog — нужно проверять, что
    teacher-режим реально доходит до CreateSchoolSG, а не молча выходит."""

    def __init__(self) -> None:
        self.started: list = []

    async def start(self, state, **kwargs) -> None:
        self.started.append(state)

    def has_context(self) -> bool:
        return False

    async def reset_stack(self, **kwargs) -> None:
        pass


# ── non-developer denial (все 3 поверхности) ───────────────────────────


async def test_non_developer_start_does_not_see_dev_menu(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "developer_ids", [777])
    msg = FakeMessage(user_id=999)
    await cmd_start(msg, FakeDialogManager())
    assert not any("Dev-режим" in r for r in msg.replies)
    async with session_scope() as s:
        assert await admins.get_by_tg_id(s, 999) is None  # обычный не-препод путь


async def test_non_developer_cannot_open_dashboard(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "developer_ids", [777])
    msg = FakeMessage(user_id=999)
    call = FakeCallback("devdash", 999, msg)
    await cb_dev_dashboard(call)
    assert msg.replies == []  # экран не отрисован
    assert call.answers == [""]  # тихий call.answer(), как у прочих non-dev гейтов


async def test_non_developer_cannot_toggle_via_callback(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "developer_ids", [777])
    msg = FakeMessage(user_id=999)
    call = FakeCallback("devmode:toggle", 999, msg)
    await cb_dev_mode_toggle(call, FakeDialogManager())
    async with session_scope() as s:
        assert await dev_ui_svc.get_ui_mode(s, 999) == "developer"  # не тронуто (дефолт)
    assert msg.replies == []


async def test_non_developer_cannot_toggle_via_command(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "developer_ids", [777])
    msg = FakeMessage(user_id=999)
    await cmd_devmode(msg, FakeDialogManager())
    async with session_scope() as s:
        assert await dev_ui_svc.get_ui_mode(s, 999) == "developer"
    assert msg.replies == []


# ── toggle persistence ──────────────────────────────────────────────────


async def test_toggle_persists_across_separate_start_calls(db, monkeypatch) -> None:
    """dev_ui_pref, не FSM/session — toggle → отдельный, independent /start
    видит новый режим."""
    monkeypatch.setattr(settings, "developer_ids", [777])
    msg1 = FakeMessage(user_id=777)
    await cmd_start(msg1, FakeDialogManager())
    assert "Dev-режим" in msg1.replies[-1]  # дефолт — developer

    toggle_call = FakeCallback("devmode:toggle", 777, FakeMessage(user_id=777))
    await cb_dev_mode_toggle(toggle_call, FakeDialogManager())
    async with session_scope() as s:
        assert await dev_ui_svc.get_ui_mode(s, 777) == "teacher"  # записано в dev_ui_pref

    dm2 = FakeDialogManager()
    msg2 = FakeMessage(user_id=777)
    await cmd_start(msg2, dm2)  # отдельный, независимый вызов /start (не сессия/FSM)
    assert not any("Dev-режим" in r for r in msg2.replies)  # режим не откатился
    async with session_scope() as s:
        assert await dev_ui_svc.get_ui_mode(s, 777) == "teacher"  # всё ещё teacher


# ── teacher-mode passthrough onboarding ─────────────────────────────────


async def test_developer_in_teacher_mode_matches_real_new_teacher_flow(db, monkeypatch) -> None:
    """Phase 10 acceptance criterion 3: developer без школы, в teacher-режиме,
    видит ТОЧНО ТОТ ЖЕ CreateSchoolSG-онбординг, что и настоящий новый
    препод — сравниваем side-by-side, не просто «не упало»."""
    monkeypatch.setattr(settings, "developer_ids", [777])

    # Оба — Admin БЕЗ школы, вставлены напрямую в обход autouse-фикстуры
    # conftest.py (она даёт каждому admin'у owned-школу через get_or_create —
    # см. память [[test-infra-owned-school-monkeypatch]]), иначе оба сразу
    # получат школу и ни один не дойдёт до CreateSchoolSG.
    async with session_scope() as s:
        s.add(Admin(tg_user_id=555, full_name="Обычный препод", has_completed_setup=False))
        s.add(Admin(tg_user_id=777, full_name="Dev", has_completed_setup=False))
    async with session_scope() as s:
        await dev_ui_svc.set_ui_mode(s, 777, "teacher")

    dm_real = FakeDialogManager()
    await cmd_start(FakeMessage(user_id=555), dm_real)

    dm_dev = FakeDialogManager()
    dev_msg = FakeMessage(user_id=777)
    await cmd_start(dev_msg, dm_dev)

    assert dm_real.started and dm_dev.started
    assert dm_real.started[0].state == dm_dev.started[0].state == CreateSchoolSG.name.state
    assert dev_msg.replies == []  # никакого спецкейс-сообщения — чистый онбординг-старт


# ── dashboard screen ─────────────────────────────────────────────────────


async def test_dashboard_renders_pulse_style_and_escapes_school_name(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "developer_ids", [777])
    async with session_scope() as s:
        teacher = await admins.get_or_create(s, 1, "t", "T")  # автошкола (conftest fixture)
        school_id = await schools.primary_school_id(s, teacher.id)
        school = await s.get(School, school_id)
        school.name = "Школа <b>XSS</b> & Co"

    msg = FakeMessage(user_id=777)
    call = FakeCallback("devdash", 777, msg)
    await cb_dev_dashboard(call)

    assert len(msg.replies) == 1
    text = msg.replies[0]
    # Pulse-примитивы (ui.title/card) — не изобретённый заново формат.
    assert "Dev dashboard" in text
    assert "Outbox / rate-limit" in text
    assert "Последний тик" in text
    assert "Нагрузка (с рестарта процесса)" in text
    # старый /devstats-контент по школам никуда не делся
    assert "админ" in text and "групп" in text
    # HTML-escape: сырой <b>/& не должен утечь неэкранированным
    assert "&lt;b&gt;XSS&lt;/b&gt; &amp; Co" in text
    assert "<b>XSS</b>" not in text
