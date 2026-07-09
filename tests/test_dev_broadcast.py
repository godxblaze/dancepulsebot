"""Phase 11: global developer broadcast — recipient-count-before-send
(≥2 scope/audience combinations vs. ground truth), confirm-step bypass
(fails safe with incomplete/tampered dialog_data), non-developer gating on
the entry point, and a check that no second Broadcast/outbox mechanism was
introduced (reuses Phase 5's exactly)."""
from types import SimpleNamespace

from sqlalchemy import func, select

from app.bot.dialogs.dev_broadcast import (
    MAX_BROADCAST_TEXT,
    _confirm_getter,
    _on_dev_broadcast_send,
    _on_text_typed,
)
from app.bot.handlers.admin import cb_dev_broadcast_start
from app.core.config import settings
from app.db.models import Admin, Base, Broadcast, MessageOutbox, Person
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import persons as persons_svc
from app.services import schools as schools_svc
from tests._outbox_helpers import drain_outbox


class FakeCall:
    def __init__(self, user_id: int, full_name: str = "Dev") -> None:
        self.from_user = SimpleNamespace(id=user_id, full_name=full_name, username="dev")
        self.message = FakeMessage()
        self.answers: list[str] = []

    async def answer(self, text: str = "", **kwargs) -> None:
        self.answers.append(text)


class FakeMessage:
    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append(text)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self._mid = 1000

    async def send_message(self, chat_id, text, **kwargs):
        self._mid += 1
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=self._mid)


class FakeManager:
    def __init__(self, dialog_data: dict) -> None:
        self.dialog_data = dialog_data
        self.done_calls = 0
        self.switch_to_calls: list = []
        self.started: list = []

    async def done(self) -> None:
        self.done_calls += 1

    async def switch_to(self, state) -> None:
        self.switch_to_calls.append(state)

    async def start(self, state, **kwargs) -> None:
        self.started.append(state)

    def has_context(self) -> bool:
        return False


async def _seed_two_schools() -> tuple[int, int]:
    """Школа A: owner + co-admin, 2 ученика. Школа B: owner, 3 ученика.
    Прямая вставка Admin в обход autouse owned-school фикстуры (нужен точный
    контроль над тем, кто в какой школе — см. память
    [[test-infra-owned-school-monkeypatch]])."""
    async with session_scope() as s:
        s.add(Admin(tg_user_id=901, full_name="Owner A"))
        s.add(Admin(tg_user_id=902, full_name="Owner B"))
        s.add(Admin(tg_user_id=903, full_name="CoAdmin A"))
    async with session_scope() as s:
        owner_a = await admins_svc.get_by_tg_id(s, 901)
        owner_b = await admins_svc.get_by_tg_id(s, 902)
        coadmin_a = await admins_svc.get_by_tg_id(s, 903)
        school_a = await schools_svc.create_school(s, owner_a.id, "Школа A")
        school_b = await schools_svc.create_school(s, owner_b.id, "Школа B")
        await schools_svc.add_coadmin(s, school_a.id, coadmin_a.id)
        await persons_svc.ensure_person(s, school_a.id, 811, "p811", "P1")
        await persons_svc.ensure_person(s, school_a.id, 812, "p812", "P2")
        await persons_svc.ensure_person(s, school_b.id, 821, "p821", "P3")
        await persons_svc.ensure_person(s, school_b.id, 822, "p822", "P4")
        await persons_svc.ensure_person(s, school_b.id, 823, "p823", "P5")
        return school_a.id, school_b.id


def _count_from_phrase(phrase: str) -> int:
    return int(phrase.split()[0])


# ── recipient count vs. ground truth ────────────────────────────────────


async def test_recipient_count_all_schools_all_persons_matches_ground_truth(db) -> None:
    await _seed_two_schools()
    async with session_scope() as s:
        ground_truth = (
            await s.execute(select(func.count()).select_from(Person))
        ).scalar_one()

    manager = FakeManager({"scope_type": "all", "audience_type": "person", "text": "Привет"})
    screen = await _confirm_getter(manager)
    assert _count_from_phrase(screen["count_phrase"]) == ground_truth == 5


async def test_recipient_count_subset_admin_only_matches_ground_truth(db) -> None:
    school_a_id, _school_b_id = await _seed_two_schools()
    # ground truth: distinct admins who are SchoolMember of school A — owner+coadmin=2
    manager = FakeManager({
        "scope_type": "subset", "school_ids": [school_a_id],
        "audience_type": "admin", "text": "Привет",
    })
    screen = await _confirm_getter(manager)
    assert _count_from_phrase(screen["count_phrase"]) == 2
    assert "Школа A" in screen["scope_desc"]
    assert "преподавателям" in screen["count_phrase"]  # audience-specific noun, не generic


async def test_recipient_count_zero_schools_is_honest_zero(db) -> None:
    manager = FakeManager({"scope_type": "all", "audience_type": "person", "text": "Привет"})
    screen = await _confirm_getter(manager)
    assert _count_from_phrase(screen["count_phrase"]) == 0


async def test_recipient_count_school_with_zero_members_is_honest_zero(db) -> None:
    async with session_scope() as s:
        s.add(Admin(tg_user_id=950, full_name="Empty Owner"))
    async with session_scope() as s:
        owner = await admins_svc.get_by_tg_id(s, 950)
        school = await schools_svc.create_school(s, owner.id, "Пустая школа")
        school_id = school.id
    manager = FakeManager({
        "scope_type": "subset", "school_ids": [school_id],
        "audience_type": "person", "text": "Привет",
    })
    screen = await _confirm_getter(manager)
    assert _count_from_phrase(screen["count_phrase"]) == 0


# ── send enqueues exactly the computed set, nothing outside scope ───────


async def test_send_enqueues_exactly_scoped_persons_excludes_other_school(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "developer_ids", [999])
    school_a_id, _school_b_id = await _seed_two_schools()
    manager = FakeManager({
        "scope_type": "subset", "school_ids": [school_a_id],
        "audience_type": "person", "text": "Привет школе A",
    })
    await _on_dev_broadcast_send(FakeCall(999), None, manager)

    broadcast_id = manager.dialog_data["broadcast_id"]
    async with session_scope() as s:
        rows = (await s.execute(
            select(MessageOutbox).where(MessageOutbox.broadcast_id == broadcast_id)
        )).scalars().all()
    chat_ids = {r.chat_id for r in rows}
    assert chat_ids == {811, 812}  # ровно ученики школы A
    assert chat_ids.isdisjoint({821, 822, 823})  # школа B — ноль строк
    assert manager.switch_to_calls  # ушли на progress


# ── confirm-step bypass fails safe ───────────────────────────────────────


async def test_send_with_empty_dialog_data_fails_safe(db, monkeypatch) -> None:
    """Прыжок сразу к send-хендлеру, минуя scope/audience/text (как если бы
    callback реплеился на «голое» состояние) — ничего не создаётся."""
    monkeypatch.setattr(settings, "developer_ids", [999])
    manager = FakeManager({})
    await _on_dev_broadcast_send(FakeCall(999), None, manager)
    async with session_scope() as s:
        assert (await s.execute(select(func.count()).select_from(Broadcast))).scalar_one() == 0
        assert (
            await s.execute(select(func.count()).select_from(MessageOutbox))
        ).scalar_one() == 0
    assert manager.done_calls == 1
    assert "sending" not in manager.dialog_data or manager.dialog_data.get("sending") is not True


async def test_send_with_partial_dialog_data_fails_safe(db, monkeypatch) -> None:
    """scope_type задан, но audience_type/text — нет (подделанный/неполный
    dialog_data) — тоже fail-safe, не отправляем."""
    monkeypatch.setattr(settings, "developer_ids", [999])
    manager = FakeManager({"scope_type": "all"})
    await _on_dev_broadcast_send(FakeCall(999), None, manager)
    async with session_scope() as s:
        assert (await s.execute(select(func.count()).select_from(Broadcast))).scalar_one() == 0
    assert manager.done_calls == 1


async def test_send_by_non_developer_is_denied_even_with_valid_data(db, monkeypatch) -> None:
    """Defense in depth: даже если бы кто-то дотянулся до хендлера с полным
    валидным dialog_data, гейт по developer_ids всё равно блокирует send."""
    monkeypatch.setattr(settings, "developer_ids", [999])
    school_a_id, _ = await _seed_two_schools()
    manager = FakeManager({
        "scope_type": "subset", "school_ids": [school_a_id],
        "audience_type": "person", "text": "Утечка",
    })
    await _on_dev_broadcast_send(FakeCall(111), None, manager)  # 111 — не dev
    async with session_scope() as s:
        assert (await s.execute(select(func.count()).select_from(Broadcast))).scalar_one() == 0


# ── non-developer cannot reach the entry point ───────────────────────────


async def test_non_developer_cannot_start_dev_broadcast_dialog(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "developer_ids", [999])
    call = FakeCall(111)
    manager = FakeManager({})
    await cb_dev_broadcast_start(call, manager)
    assert manager.started == []  # диалог не стартовал
    assert call.answers == [""]  # тихий call.answer(), как у прочих non-dev гейтов


async def test_developer_can_start_dev_broadcast_dialog(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "developer_ids", [999])
    call = FakeCall(999)
    manager = FakeManager({})
    await cb_dev_broadcast_start(call, manager)
    assert len(manager.started) == 1


# ── exactly one Broadcast model / message_outbox table ──────────────────


def test_exactly_one_broadcast_model_and_outbox_table() -> None:
    broadcast_mappers = [
        m for m in Base.registry.mappers if m.class_.__name__ == "Broadcast"
    ]
    assert len(broadcast_mappers) == 1
    assert "message_outbox" in Base.metadata.tables
    assert "broadcasts" in Base.metadata.tables


# ── Phase 12 edges: long text, HTML-injection ────────────────────────────


async def test_broadcast_text_too_long_is_rejected() -> None:
    manager = FakeManager({})
    msg = FakeMessage()
    too_long = "a" * (MAX_BROADCAST_TEXT + 1)
    await _on_text_typed(msg, None, manager, too_long)
    assert "text" not in manager.dialog_data  # не сохранён
    assert manager.switch_to_calls == []  # не ушли на confirm
    assert msg.answers and "Слишком длинно" in msg.answers[0]


async def test_broadcast_text_at_limit_is_accepted() -> None:
    manager = FakeManager({})
    msg = FakeMessage()
    at_limit = "a" * MAX_BROADCAST_TEXT
    await _on_text_typed(msg, None, manager, at_limit)
    assert manager.dialog_data["text"] == at_limit
    assert manager.switch_to_calls  # ушли на confirm


async def test_broadcast_html_injection_escaped_in_delivered_message(db, monkeypatch) -> None:
    """Текст с `<script>`/`&` должен дойти до получателя экранированным
    (HTML-сущностями), не как рабочая разметка — реальная доставка через
    drain_outbox, не просто рендер confirm-экрана."""
    monkeypatch.setattr(settings, "developer_ids", [999])
    school_a_id, _ = await _seed_two_schools()
    injected = '<script>alert(1)</script> & "quotes" <b>bold</b>'
    manager = FakeManager({
        "scope_type": "subset", "school_ids": [school_a_id],
        "audience_type": "person", "text": injected,
    })
    await _on_dev_broadcast_send(FakeCall(999), None, manager)

    bot = FakeBot()
    delivered = await drain_outbox(bot)
    assert delivered == 2  # P1 + P2 школы A
    assert bot.sent
    for _chat_id, text in bot.sent:
        assert "<script>" not in text
        assert "&lt;script&gt;" in text
        assert "&amp;" in text
        assert "<b>bold</b>" not in text
        assert "&lt;b&gt;bold&lt;/b&gt;" in text
