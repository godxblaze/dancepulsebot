from __future__ import annotations

import asyncio
import weakref
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import Counter
from app.db.models import Gender, Person, SupportCall, SupportCallStatus, SupportClaim, Survey
from app.services.schedule import as_utc

# Phase 9 observability — сколько раз claim() отклонил тап не из-за повторного
# тапа тем же человеком, а потому что слот/call уже закрылся до него (реальная
# гонка, не идемпотентный re-tap). См. claim() ниже.
_claim_conflicts = Counter()


def claim_conflict_count() -> int:
    return _claim_conflicts.value

# Однопроцессный бот (polling) обрабатывает апдейты конкурентными задачами: два
# «Приду помочь» на последний слот могут прочитать одинаковый count и перебрать
# набор. Пер-call asyncio.Lock сериализует claim/dismiss одного call'а от чтения
# счётчика до commit'а вызывающей сессии. Вызывающий держит его как
# `async with supports.call_lock(call_id):` ВОКРУГ своего session_scope — иначе
# commit (и, значит, видимость вставки другим сессиям) окажется вне блокировки.
# Ключ — сам loop (не id(loop)): asyncio.Lock привязан к loop'у, на котором был
# впервые захвачен, и падает при переиспользовании в другом (в тестах pytest-asyncio
# даёт свой loop на тест). WeakKeyDictionary + сам объект loop'а, а не его id(),
# исключает коллизию, если Python переиспользует id() сборранного GC loop'а под
# новый объект между тестами. В проде loop один навсегда → эффективно пер-call.
_call_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Lock]] = (
    weakref.WeakKeyDictionary()
)


def call_lock(call_id: int) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    per_loop = _call_locks.get(loop)
    if per_loop is None:
        per_loop = {}
        _call_locks[loop] = per_loop
    lock = per_loop.get(call_id)
    if lock is None:
        lock = asyncio.Lock()
        per_loop[call_id] = lock
    return lock


async def _recount_helped(session: AsyncSession, person_id: int) -> None:
    """helped_count = число claim'ов этого человека с attended=True. Денормализованный
    счётчик пересчитывается из факта явки, не из числа откликов."""
    cnt = (await session.execute(
        select(func.count())
        .select_from(SupportClaim)
        .where(SupportClaim.person_id == person_id, SupportClaim.attended.is_(True))
    )).scalar_one()
    person = await session.get(Person, person_id)
    if person is not None:
        person.helped_count = cnt
        await session.flush()


async def get_call(session: AsyncSession, call_id: int) -> SupportCall | None:
    return await session.get(SupportCall, call_id)


async def get_call_for_survey(
    session: AsyncSession, survey_id: int
) -> SupportCall | None:
    stmt = select(SupportCall).where(SupportCall.survey_id == survey_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_call(
    session: AsyncSession,
    survey_id: int,
    gender: Gender,
    needed: int,
    status: SupportCallStatus,
) -> SupportCall:
    call = SupportCall(survey_id=survey_id, gender=gender, needed=needed, status=status)
    session.add(call)
    await session.flush()
    return call


async def set_status(
    session: AsyncSession, call_id: int, status: SupportCallStatus
) -> None:
    call = await session.get(SupportCall, call_id)
    if call is not None:
        call.status = status
        await session.flush()


async def dismiss_call(session: AsyncSession, call_id: int) -> bool:
    """Lead закрыл picker кнопкой «Баланс ок». Закрываем call
    (status closed = «nothing was needed»), но только если он ещё pending —
    иначе гонка с уже отправленными инвайтами (picksend). Ставим topup_done,
    чтобы фаза-2 добора остатка не воскресила call и не прислала picker снова
    при подросшем дефиците: «не сейчас» = решение на всё занятие (препод всегда
    может позвать вручную через 🆘 Саппорты). True, если закрыли."""
    call = await session.get(SupportCall, call_id)
    if call is None or call.status != SupportCallStatus.pending:
        return False
    call.status = SupportCallStatus.closed
    call.topup_done = True
    await session.flush()
    return True


async def count_claims(session: AsyncSession, call_id: int) -> int:
    stmt = (
        select(func.count())
        .select_from(SupportClaim)
        .where(SupportClaim.support_call_id == call_id)
    )
    return (await session.execute(stmt)).scalar_one()


async def claimed_person_ids(session: AsyncSession, call_id: int) -> set[int]:
    """Person id'ы, уже согласившиеся выручить по этому call'у — чтобы не
    предлагать их повторно в picker'е добора остатка (фаза 2)."""
    stmt = select(SupportClaim.person_id).where(
        SupportClaim.support_call_id == call_id
    )
    return set((await session.execute(stmt)).scalars().all())


async def unclaim(
    session: AsyncSession, call_id: int, person_id: int, *, now: datetime
) -> bool:
    """Revert a person's claim. Re-opens a closed call so the freed slot is
    available again, and recomputes helped_count. Returns True if anything changed.

    Defense-in-depth: отказывает, если занятие уже прошло (`now` — момент,
    которым сам вызывающий уже гейтит хендлер-гвард в cb_support_skip; сервис
    перепроверяет его же, а не доверяет хендлеру вслепую — claim() зеркально
    гардит по call.status). `SELECT ... FOR UPDATE` на SupportCall — тот же
    принцип, что и в claim() (Phase 12): безопасно даже без call_lock."""
    row = (
        await session.execute(
            select(SupportClaim).where(
                SupportClaim.support_call_id == call_id,
                SupportClaim.person_id == person_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    call = (
        await session.execute(
            select(SupportCall).where(SupportCall.id == call_id).with_for_update()
        )
    ).scalar_one_or_none()
    if call is not None:
        survey = await session.get(Survey, call.survey_id)
        if survey is not None and as_utc(survey.lesson_at) <= now:
            return False  # занятие уже прошло — не откатываем явку задним числом
    # Заполненность ДО удаления — единственный сигнал «почему closed» без
    # отдельного поля: если filled>=needed, call закрылся именно от заполнения
    # нормы, и освободившийся слот реально свободен. Если filled<needed при
    # closed — это был dismiss («Не сейчас») или phase-2 «баланс сложился»
    # (сознательное решение закрыть НЕСМОТРЯ на недобор); безусловный reopen
    # тут воскресил бы call, который лид/система закрыли намеренно.
    filled_before = await count_claims(session, call_id) if call is not None else 0
    await session.delete(row)
    await session.flush()
    await _recount_helped(session, person_id)
    if (
        call is not None
        and call.status == SupportCallStatus.closed
        and filled_before >= call.needed
    ):
        call.status = SupportCallStatus.open
    await session.flush()
    return True


async def set_picker_ref(
    session: AsyncSession,
    call_id: int,
    chat_id: int,
    message_id: int,
    *,
    has_keyboard: bool = False,
) -> None:
    """B8: запомнить (chat_id, message_id) recruit-сообщения lead'а — чтобы потом
    обновлять в нём баланс-строку при claim/skip. has_keyboard=True — сообщение
    несёт живой picker (кнопки выбора саппортов), не просто текст-хедсап; такое
    сообщение _refresh_balance не трогает (см. app/bot/handlers/recruit.py)."""
    call = await session.get(SupportCall, call_id)
    if call is not None:
        call.picker_chat_id = chat_id
        call.picker_message_id = message_id
        call.picker_has_keyboard = has_keyboard
        await session.flush()


async def mark_invited(session: AsyncSession, call_id: int) -> None:
    """Инвайты только что отправлены: статус → open, таймер «тишины» считаем с
    этого момента (created_at = now), флаг no_reply_notified сбрасываем."""
    call = await session.get(SupportCall, call_id)
    if call is not None:
        call.status = SupportCallStatus.open
        call.created_at = datetime.now(UTC).replace(tzinfo=None)
        call.no_reply_notified = False
        await session.flush()


async def pending_unnotified_calls(session: AsyncSession) -> list[SupportCall]:
    """Pending-call'ы (owner получил picker, но ещё не нажал «Позвать»), по
    которым его ещё не пинговали. Scheduler решает по близости занятия, пора ли
    напомнить «разбери добор»."""
    stmt = select(SupportCall).where(
        SupportCall.status == SupportCallStatus.pending,
        SupportCall.pending_notified.is_(False),
    )
    return list((await session.execute(stmt)).scalars().all())


async def mark_pending_notified(session: AsyncSession, call_id: int) -> None:
    call = await session.get(SupportCall, call_id)
    if call is not None:
        call.pending_notified = True
        await session.flush()


async def claimed_supports_for_survey(
    session: AsyncSession, survey_id: int
) -> list[tuple[Person, SupportClaim]]:
    """Согласившиеся саппорты занятия (через SupportCall) — для пост-leson
    отметки явки. Возвращает (person, claim), по имени."""
    stmt = (
        select(Person, SupportClaim)
        .join(SupportClaim, SupportClaim.person_id == Person.id)
        .join(SupportCall, SupportCall.id == SupportClaim.support_call_id)
        .where(SupportCall.survey_id == survey_id)
        .order_by(Person.full_name)
    )
    return [(p, c) for p, c in (await session.execute(stmt)).all()]


async def set_support_attended(
    session: AsyncSession, claim_id: int, attended: bool | None
) -> int | None:
    """Отметить, пришёл ли саппорт. Пересчитывает helped_count человека.
    Возвращает person_id (или None если claim не найден)."""
    claim = await session.get(SupportClaim, claim_id)
    if claim is None:
        return None
    claim.attended = attended
    await session.flush()
    await _recount_helped(session, claim.person_id)
    return claim.person_id


async def stale_open_calls(
    session: AsyncSession, now: datetime, idle_for: timedelta
) -> list[SupportCall]:
    """Open SupportCall'ы, созданные раньше чем now-idle_for, у которых ещё
    нет required-числа отклинков и preпод ещё не был уведомлён про тишину.

    Используется scheduler'ом чтобы сказать preподу «никто не откликнулся за час»."""
    cutoff = now - idle_for
    stmt = (
        select(SupportCall)
        .where(
            SupportCall.status == SupportCallStatus.open,
            SupportCall.no_reply_notified.is_(False),
            SupportCall.created_at < cutoff,
        )
    )
    return list((await session.execute(stmt)).scalars().all())


async def mark_no_reply_notified(session: AsyncSession, call_id: int) -> None:
    call = await session.get(SupportCall, call_id)
    if call is not None:
        call.no_reply_notified = True
        await session.flush()


async def claim(
    session: AsyncSession, call_id: int, person_id: int
) -> tuple[bool, int, int]:
    """Record a support claim if there is room.

    Returns (accepted, filled, needed). `accepted` is False if the slot is already
    full, the person claimed before, or the call is closed (filled elsewhere or
    dismissed). Closes the call and bumps the person's helped_count when the
    claim succeeds.

    `SELECT ... FOR UPDATE` on the SupportCall row (not `session.get`) — the
    in-process `call_lock` (see module docstring) only serializes concurrent
    taps within ONE bot process; this row lock makes `claim()` itself safe
    even if called without it (defense in depth — Phase 12 load-test replay
    found bare `claim()` overbooked 17/5 under 30 concurrent callers with no
    DB-level lock at all)."""
    call = (
        await session.execute(
            select(SupportCall).where(SupportCall.id == call_id).with_for_update()
        )
    ).scalar_one_or_none()
    if call is None:
        return False, 0, 0
    if call.status not in (SupportCallStatus.pending, SupportCallStatus.open):
        # Закрыт (заполнен либо dismiss'нут лидом) — новые отклики не принимаем.
        filled = await count_claims(session, call_id)
        _claim_conflicts.inc()
        return False, filled, call.needed
    dup = await session.execute(
        select(SupportClaim).where(
            SupportClaim.support_call_id == call_id,
            SupportClaim.person_id == person_id,
        )
    )
    filled = await count_claims(session, call_id)
    if dup.scalar_one_or_none() is not None:
        return False, filled, call.needed  # тот же человек повторно — не гонка
    if filled >= call.needed:
        _claim_conflicts.inc()
        return False, filled, call.needed
    # helped_count НЕ инкрементим здесь — он считается по факту явки
    # (SupportClaim.attended), которую preпод отмечает после занятия.
    session.add(SupportClaim(support_call_id=call_id, person_id=person_id))
    filled += 1
    if filled >= call.needed:
        call.status = SupportCallStatus.closed
    await session.flush()
    return True, filled, call.needed
