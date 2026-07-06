"""B16: recruit_mode — enum, миграция, сеттер (Phase 1) + режимы scheduler
(Phase 2: curated / open_race / auto_top)."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.bot.handlers.recruit import cb_support_pick_nope
from app.db.models import Answer, Gender, RecruitMode, SupportCallStatus, SurveyKind
from app.db.session import _apply_lightweight_migrations, engine, session_scope
from app.scheduler import scheduler as sched
from app.services import admins as admins_svc
from app.services import groups, persons, schools, supports, surveys
from tests._fakes import FakeCallback, fake_user

_MSK = ZoneInfo("Europe/Moscow")
# Занятие через 14 дней, 19:00 МСК (как в test_recruit). -3ч заведомо после
# вечернего разбора (дефолт 20:00 накануне).
LESSON = (
    (datetime.now(UTC).astimezone(_MSK) + timedelta(days=14))
    .replace(hour=19, minute=0, second=0, microsecond=0)
    .astimezone(UTC)
)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs) -> None:
        self.sent.append((chat_id, text))


def test_recruit_mode_enum_has_four_values() -> None:
    assert {m.value for m in RecruitMode} == {
        "curated",
        "open_race",
        "auto_top",
        "escalate",
    }


async def test_default_recruit_mode_is_curated(db) -> None:
    async with session_scope() as s:
        admin = await admins_svc.get_or_create(s, 100, "u", "Препод")
        assert admin.recruit_mode == RecruitMode.curated.value


async def test_set_recruit_mode_persists(db) -> None:
    async with session_scope() as s:
        admin = await admins_svc.get_or_create(s, 101, "u", "Препод")
        ok = await admins_svc.set_recruit_mode(s, admin.id, "open_race")
        assert ok is True
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, 101)
        assert admin.recruit_mode == "open_race"


async def test_set_recruit_mode_rejects_invalid(db) -> None:
    async with session_scope() as s:
        admin = await admins_svc.get_or_create(s, 102, "u", "Препод")
        ok = await admins_svc.set_recruit_mode(s, admin.id, "bogus")
        assert ok is False
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, 102)
        assert admin.recruit_mode == RecruitMode.curated.value


async def test_migration_adds_recruit_mode_to_old_db(db) -> None:
    """Старая БД без колонки → миграция добавляет её с дефолтом curated;
    существующие строки получают curated."""
    async with session_scope() as s:
        admin = await admins_svc.get_or_create(s, 103, "u", "Препод")
        admin_id = admin.id

    async with engine.begin() as conn:
        # Симулируем старую БД: убираем колонку.
        await conn.execute(text("ALTER TABLE admins DROP COLUMN recruit_mode"))
        cols = {
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(admins)"))).fetchall()
        }
        assert "recruit_mode" not in cols
        # Миграция должна вернуть колонку и проставить дефолт существующей строке.
        added = await _apply_lightweight_migrations(conn)
        assert ("admins", "recruit_mode") in added
        value = (
            await conn.execute(
                text("SELECT recruit_mode FROM admins WHERE id = :i"), {"i": admin_id}
            )
        ).scalar_one()
    assert value == "curated"


# ── Phase 2: режимы scheduler ────────────────────────────────────────


async def _lesson_needing_two_males(mode: str):
    """Группа с дефицитом 2 парней; lead(tg100) с режимом `mode`; личный пул из
    3 парней (уровни 3/2/1) и коллега(tg555) с одним парнем-саппортом."""
    async with session_scope() as s:
        t = await admins_svc.get_or_create(s, 100, "teach", "Препод")
        await admins_svc.set_recruit_mode(s, t.id, mode)
        g = await groups.create_group(s, t.id, "Бачата", "0", "19:00", 18)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, LESSON
        )
        uid = 200
        for gender, count in ((Gender.male, 2), (Gender.female, 4)):  # дефицит 2
            for _ in range(count):
                p, _ = await persons.add_to_group(s, g, uid, "u", f"P{uid}")
                await persons.set_gender(s, p.id, gender)
                await surveys.record_answer(s, survey.id, p.id, Answer.yes)
                uid += 1
        pool_group = await groups.create_group(s, t.id, "Пул", "2", "20:00", 18)
        pool: dict[int, int] = {}
        for lvl, tg in ((3, 301), (2, 302), (1, 303)):
            p, _ = await persons.add_to_group(s, pool_group, tg, "u", f"Пул{lvl}")
            await persons.set_gender(s, p.id, Gender.male)
            await persons.set_pool_level(s, t.id, p.id, lvl)
            pool[lvl] = tg
        co = await admins_svc.get_or_create(s, 555, "c", "Коллега")
        await schools.add_coadmin(s, g.school_id, co.id)
        co_p, _ = await persons.add_to_group(s, pool_group, 401, "u", "Коллега-пул")
        await persons.set_gender(s, co_p.id, Gender.male)
        await persons.set_pool_level(s, co.id, co_p.id, 2)
        return SimpleNamespace(
            group=g, survey_id=survey.id, lead_tg=100, pool=pool, colleague_tg=401
        )


async def _imbalanced_no_pool(mode: str):
    """Дефицит 3 парней, пустой пул lead'а."""
    async with session_scope() as s:
        t = await admins_svc.get_or_create(s, 100, "teach", "Препод")
        await admins_svc.set_recruit_mode(s, t.id, mode)
        g = await groups.create_group(s, t.id, "Бачата", "0", "19:00", 18)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, LESSON
        )
        uid = 200
        for gender, count in ((Gender.male, 1), (Gender.female, 4)):
            for _ in range(count):
                p, _ = await persons.add_to_group(s, g, uid, "u", f"P{uid}")
                await persons.set_gender(s, p.id, gender)
                await surveys.record_answer(s, survey.id, p.id, Answer.yes)
                uid += 1
        return g, survey.id


_AFTER_FIX = LESSON - timedelta(hours=3)


async def test_curated_mode_keeps_pending_picker(db) -> None:
    """Регресс: curated (дефолт) — pending-call + picker lead'у, без авто-рассылки."""
    ctx = await _lesson_needing_two_males("curated")
    bot = FakeBot()
    await sched._maybe_recruit(bot, ctx.group, LESSON, _AFTER_FIX)
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, ctx.survey_id)
        assert call is not None and call.status == SupportCallStatus.pending
    # picker ушёл lead'у; пулу ничего не рассылалось
    assert all(chat == ctx.lead_tg for chat, _ in bot.sent)
    assert any("отметь, кого позвать" in t.lower() for _, t in bot.sent)


async def test_open_race_invites_pool_and_colleagues_no_picker(db) -> None:
    ctx = await _lesson_needing_two_males("open_race")
    bot = FakeBot()
    await sched._maybe_recruit(bot, ctx.group, LESSON, _AFTER_FIX)
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, ctx.survey_id)
        assert call is not None and call.status == SupportCallStatus.open
    invited = {chat for chat, t in bot.sent if "Нужна помощь" in t}
    # приглашены все подходящие: весь пул lead'а + саппорт коллеги
    assert invited == {ctx.pool[3], ctx.pool[2], ctx.pool[1], ctx.colleague_tg}
    lead_msgs = [t for chat, t in bot.sent if chat == ctx.lead_tg]
    assert any("открытый добор" in t.lower() for t in lead_msgs)
    assert not any("отметь, кого позвать" in t.lower() for t in lead_msgs)  # picker нет


async def test_auto_top_invites_top_n_by_level(db) -> None:
    ctx = await _lesson_needing_two_males("auto_top")
    bot = FakeBot()
    await sched._maybe_recruit(bot, ctx.group, LESSON, _AFTER_FIX)
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, ctx.survey_id)
        assert call is not None and call.status == SupportCallStatus.open
    invited = {chat for chat, t in bot.sent if "Нужна помощь" in t}
    # дефицит 2 → ровно top-2 по уровню: уровень 3 и 2; уровень 1 НЕ зван
    assert invited == {ctx.pool[3], ctx.pool[2]}
    assert ctx.pool[1] not in invited
    assert ctx.colleague_tg not in invited  # auto_top — только личный пул lead'а
    lead_msgs = [t for chat, t in bot.sent if chat == ctx.lead_tg]
    assert any("позвал автоматически" in t.lower() for t in lead_msgs)


async def test_open_race_empty_pool_graceful(db) -> None:
    g, survey_id = await _imbalanced_no_pool("open_race")
    bot = FakeBot()
    await sched._maybe_recruit(bot, g, LESSON, _AFTER_FIX)  # не падает
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, survey_id)
        assert call is not None and call.status == SupportCallStatus.open
    assert any("нет подходящих" in t.lower() for chat, t in bot.sent if chat == 100)


async def test_auto_top_empty_pool_graceful(db) -> None:
    g, survey_id = await _imbalanced_no_pool("auto_top")
    bot = FakeBot()
    await sched._maybe_recruit(bot, g, LESSON, _AFTER_FIX)  # не падает
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, survey_id)
        assert call is not None and call.status == SupportCallStatus.open
    assert any("нет подходящих" in t.lower() for chat, t in bot.sent if chat == 100)


async def test_top_candidates_orders_by_level_and_limits(db) -> None:
    async with session_scope() as s:
        t = await admins_svc.get_or_create(s, 100, "t", "T")
        g = await groups.create_group(s, t.id, "G", "0", "19:00", 18)
        other = await groups.create_group(s, t.id, "Пул", "2", "20:00", 18)
        ids: dict[int, int] = {}
        for lvl, tg in ((1, 401), (3, 402), (2, 403)):
            p, _ = await persons.add_to_group(s, other, tg, "u", f"P{lvl}")
            await persons.set_gender(s, p.id, Gender.male)
            await persons.set_pool_level(s, t.id, p.id, lvl)
            ids[lvl] = p.id
        top = await persons.top_candidates(s, t.id, Gender.male, g.id, 2)
        assert [p.id for p in top] == [ids[3], ids[2]]  # top-2 по уровню desc


# ── Phase 3: режим escalate ──────────────────────────────────────────

# До окна эскалации (ESCALATE_LEAD=6ч) — picker как curated.
_BEFORE_ESC = LESSON - timedelta(hours=20)
# В окне эскалации, но до top-up окна (3ч) — чистая эскалация.
_IN_ESC = LESSON - timedelta(hours=4)


async def _escalate_pending(ctx) -> None:
    """Создаёт pending-picker (escalate до окна эскалации = curated)."""
    await sched._maybe_recruit(FakeBot(), ctx.group, LESSON, _BEFORE_ESC)


async def test_escalate_before_window_behaves_as_curated(db) -> None:
    ctx = await _lesson_needing_two_males("escalate")
    bot = FakeBot()
    await sched._maybe_recruit(bot, ctx.group, LESSON, _BEFORE_ESC)
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, ctx.survey_id)
        assert call is not None and call.status == SupportCallStatus.pending
    assert any("отметь, кого позвать" in t.lower() for _, t in bot.sent)  # picker
    assert not any("Нужна помощь" in t for _, t in bot.sent)  # без авто-рассылки
    # повторный tick всё ещё до окна (lesson-7h) — ничего нового
    bot2 = FakeBot()
    await sched._maybe_recruit(bot2, ctx.group, LESSON, LESSON - timedelta(hours=7))
    assert bot2.sent == []


async def test_escalate_pending_auto_dispatches_in_window(db) -> None:
    ctx = await _lesson_needing_two_males("escalate")
    await _escalate_pending(ctx)  # pending picker
    bot = FakeBot()
    await sched._maybe_recruit(bot, ctx.group, LESSON, _IN_ESC)  # окно эскалации
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, ctx.survey_id)
        assert call is not None and call.status == SupportCallStatus.open  # pending→open
    invited = {chat for chat, t in bot.sent if "Нужна помощь" in t}
    # open-race fallback: весь подходящий пул + коллега
    assert invited == {ctx.pool[3], ctx.pool[2], ctx.pool[1], ctx.colleague_tg}


async def test_escalate_does_not_fire_twice(db) -> None:
    ctx = await _lesson_needing_two_males("escalate")
    await _escalate_pending(ctx)
    bot = FakeBot()
    await sched._maybe_recruit(bot, ctx.group, LESSON, _IN_ESC)  # эскалирует
    assert sum(1 for _, t in bot.sent if "Нужна помощь" in t) == 4
    # повторный tick: call уже open (гард статус-перехода) → новой рассылки нет
    bot2 = FakeBot()
    await sched._maybe_recruit(bot2, ctx.group, LESSON, _IN_ESC)
    assert not any("Нужна помощь" in t for _, t in bot2.sent)


async def test_escalate_and_picknope_concurrent_consistent(db) -> None:
    """Пилот-аудит: scheduler-эскалация (pending→open + инвайты) раньше мутировала
    call без call_lock, которым обвязаны tap-хендлеры — «лид тапает ‘Не сейчас’ в
    то же мгновение, что scheduler эскалирует» могло перезаписать dismiss открытием
    и разослать инвайты вопреки решению препода. Теперь оба пути берут
    call_lock(call_id): исход согласован — либо dismiss выиграл (closed, эскалация
    видит не-pending и молчит), либо эскалация выиграла (open + инвайты, dismiss
    находит call уже не pending и отказывает). Никогда «инвайты ушли, но
    call закрыт как ‘не сейчас’»."""
    ctx = await _lesson_needing_two_males("escalate")
    await _escalate_pending(ctx)  # pending picker
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, ctx.survey_id)
        call_id = call.id
    bot = FakeBot()
    nope = FakeCallback(f"picknope:{call_id}", fake_user(ctx.lead_tg))
    await asyncio.gather(
        sched._maybe_recruit(bot, ctx.group, LESSON, _IN_ESC),
        cb_support_pick_nope(nope),
    )
    async with session_scope() as s:
        call = await supports.get_call(s, call_id)
    invited = any("Нужна помощь" in t for _, t in bot.sent)
    if call.status == SupportCallStatus.closed:
        assert not invited  # dismiss выиграл — эскалация не проскочила следом
    else:
        assert call.status == SupportCallStatus.open and invited  # эскалация выиграла
    await engine.dispose()  # общая файловая тест-БД: закрыть пул после конкуренции


async def test_curated_does_not_escalate(db) -> None:
    ctx = await _lesson_needing_two_males("curated")
    await _escalate_pending(ctx)  # pending picker
    bot = FakeBot()
    await sched._maybe_recruit(bot, ctx.group, LESSON, _IN_ESC)  # окно эскалации
    assert not any("Нужна помощь" in t for _, t in bot.sent)  # эскалации нет
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, ctx.survey_id)
        assert call is not None and call.status == SupportCallStatus.pending  # как было


async def test_open_race_open_call_does_not_escalate(db) -> None:
    """open_race/auto_top создают open-call (не pending) → эскалация не применима."""
    ctx = await _lesson_needing_two_males("open_race")
    await sched._maybe_recruit(FakeBot(), ctx.group, LESSON, _IN_ESC)  # phase1 open+invites
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, ctx.survey_id)
        assert call.status == SupportCallStatus.open
    bot = FakeBot()
    await sched._maybe_recruit(bot, ctx.group, LESSON, _IN_ESC)  # повтор в окне эскалации
    assert not any("Нужна помощь" in t for _, t in bot.sent)  # без повторной рассылки


async def test_escalate_excludes_already_claimed_candidate(db) -> None:
    """Фильтр эскалации (по образцу фазы-2, supports.claimed_person_ids): кто
    уже согласился по этому call'у, повторный инвайт при авто-фолбэке не получает."""
    ctx = await _lesson_needing_two_males("escalate")
    await _escalate_pending(ctx)  # pending picker
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, ctx.survey_id)
        claimed_person = await persons.get_person(s, ctx.group.school_id, ctx.pool[3])
        accepted, _, _ = await supports.claim(s, call.id, claimed_person.id)
        assert accepted is True  # pending — claim() это разрешает
    bot = FakeBot()
    await sched._maybe_recruit(bot, ctx.group, LESSON, _IN_ESC)  # эскалация
    invited = {chat for chat, t in bot.sent if "Нужна помощь" in t}
    assert ctx.pool[3] not in invited  # уже согласившийся — не зван повторно
    assert invited == {ctx.pool[2], ctx.pool[1], ctx.colleague_tg}


async def test_escalate_coexists_with_topup(db) -> None:
    ctx = await _lesson_needing_two_males("escalate")  # topup 3ч включён (дефолт)
    await _escalate_pending(ctx)
    bot = FakeBot()
    await sched._maybe_recruit(bot, ctx.group, LESSON, _IN_ESC)  # эскалация
    assert sum(1 for _, t in bot.sent if "Нужна помощь" in t) == 4
    # top-up окно (lesson-2h): обрабатывается без падений и без повторной рассылки
    bot2 = FakeBot()
    await sched._maybe_recruit(bot2, ctx.group, LESSON, LESSON - timedelta(hours=2))
    assert not any("Нужна помощь" in t for _, t in bot2.sent)
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, ctx.survey_id)
        assert call.status == SupportCallStatus.open  # уже добираем достаточно
        assert call.topup_done is True  # top-up отработал поверх escalate


async def _balanced_lesson_with_female_pool(mode: str):
    """3 vs 3 (баланс), потом две девушки отменяются → дыра 2 девушки. Пул
    lead'а — 2 девушки-саппорта, чтобы фаза 2 было кого звать."""
    async with session_scope() as s:
        t = await admins_svc.get_or_create(s, 100, "teach", "Препод")
        await admins_svc.set_recruit_mode(s, t.id, mode)
        g = await groups.create_group(s, t.id, "Бачата", "0", "19:00", 18)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, LESSON
        )
        uid = 200
        for gender, count in ((Gender.male, 3), (Gender.female, 3)):
            for _ in range(count):
                p, _ = await persons.add_to_group(s, g, uid, "u", f"P{uid}")
                await persons.set_gender(s, p.id, gender)
                await surveys.record_answer(s, survey.id, p.id, Answer.yes)
                uid += 1
        pool_group = await groups.create_group(s, t.id, "Пул", "2", "20:00", 18)
        for tg in (901, 902):
            fp, _ = await persons.add_to_group(s, pool_group, tg, "u", f"F{tg}")
            await persons.set_gender(s, fp.id, Gender.female)
            await persons.set_pool_level(s, t.id, fp.id, 2)
        return SimpleNamespace(group=g, survey_id=survey.id)


async def test_escalate_topup_picker_gets_grace_before_escalating(db) -> None:
    """Свежий picker, переоткрытый фазой 2 под escalate (support_cutoff_hours=3ч
    по умолчанию, раньше ESCALATE_LEAD=6ч), не должен эскалировать на первом же
    следующем тике — lead получает ESCALATE_MIN_GRACE, чтобы среагировать."""
    ctx = await _balanced_lesson_with_female_pool("escalate")
    bot = FakeBot()
    await sched._maybe_recruit(bot, ctx.group, LESSON, LESSON - timedelta(hours=20))
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, ctx.survey_id)
        assert call.status == SupportCallStatus.closed  # вечером баланс ок
        females = [
            p for p in await persons.group_members(s, ctx.group.id)
            if p.gender == Gender.female
        ]
        for p in females[:2]:
            await surveys.record_answer(s, ctx.survey_id, p.id, Answer.no)

    # фаза 2 переоткрывает call под свежую дыру (topup_hours=3ч по умолчанию)
    await sched._maybe_recruit(bot, ctx.group, LESSON, LESSON - timedelta(hours=3))
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, ctx.survey_id)
        assert call.status == SupportCallStatus.pending
        assert call.topup_done is True

    # следующий тик (~15 мин) — до бага эскалировало бы немедленно
    bot2 = FakeBot()
    await sched._maybe_recruit(
        bot2, ctx.group, LESSON, LESSON - timedelta(hours=2, minutes=45)
    )
    assert not any("Нужна помощь" in t for _, t in bot2.sent)
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, ctx.survey_id)
        assert call.status == SupportCallStatus.pending  # ещё не эскалировал

    # grace (1ч) прошёл — теперь эскалирует как обычно
    bot3 = FakeBot()
    await sched._maybe_recruit(
        bot3, ctx.group, LESSON, LESSON - timedelta(hours=1, minutes=45)
    )
    assert any("Нужна помощь" in t for _, t in bot3.sent)
    async with session_scope() as s:
        call = await supports.get_call_for_survey(s, ctx.survey_id)
        assert call.status == SupportCallStatus.open

