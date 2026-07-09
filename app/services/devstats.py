"""Per-school агрегация вовлечённости за окно WINDOW — единый снапшот для
/devstats, scripts/dev_stats.py и scripts/stats_dashboard.py: формат вывода у
каждого свой, цифры считаются здесь и потому совпадают.

Phase 9 добавляет сюда же (единая точка входа, не отдельный модуль) outbox-
здоровье/счётчики отправки и тонкие passthrough'и к in-process live-gauge'ам
(db session duration, callback latency, support-claim-конфликты) — те живут
рядом с самой инструментацией (app/db/session.py, app/bot/middlewares.py,
app/services/supports.py), но собираются здесь, как и всё остальное."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.middlewares import callback_latency_stats as _callback_latency_stats
from app.core.metrics import last_tick as _last_tick
from app.db.models import (
    Answer,
    Group,
    MessageOutbox,
    Response,
    School,
    SchoolMember,
    SupportCall,
    SupportCallStatus,
    Survey,
)
from app.db.session import session_duration_stats as _session_duration_stats
from app.services.supports import claim_conflict_count as _claim_conflict_count

WINDOW = timedelta(weeks=4)


async def school_row(session: AsyncSession, school: School, since: datetime) -> dict:
    """Срез одной школы: масштаб (админы/активные группы), последняя
    активность (aware-UTC | None), отклик/посещаемость/no-show за окно
    и зависший добор."""
    n_admins = (await session.execute(
        select(func.count()).select_from(SchoolMember)
        .where(SchoolMember.school_id == school.id)
    )).scalar_one()
    n_groups = (await session.execute(
        select(func.count()).select_from(Group)
        .where(Group.school_id == school.id, Group.active.is_(True))
    )).scalar_one()
    last_activity = (await session.execute(
        select(func.max(Survey.created_at))
        .select_from(Survey)
        .join(Group, Survey.group_id == Group.id)
        .where(
            Group.school_id == school.id,
            # Backfill (scheduler.backfill_missed_surveys) заводит closed Survey
            # без единой Response-строки задним числом — его created_at=«сейчас»
            # ложно омолодил бы стухшую школу. Считаем активностью только
            # survey, по которому реально кого-то опросили.
            select(Response.id).where(Response.survey_id == Survey.id).exists(),
        )
    )).scalar_one()
    # SQLite отдаёт naive-UTC — нормализуем к aware (конвенция persons.py),
    # иначе потребители падают на сравнении с aware-датами.
    if last_activity is not None and last_activity.tzinfo is None:
        last_activity = last_activity.replace(tzinfo=UTC)
    total, yes, marked, attended, noshow = (await session.execute(
        select(
            func.count(Response.id),
            func.sum(case((Response.answer == Answer.yes, 1), else_=0)),
            func.sum(case((Response.actually_attended.is_not(None), 1), else_=0)),
            func.sum(case((Response.actually_attended.is_(True), 1), else_=0)),
            func.sum(case((
                (Response.answer == Answer.yes)
                & Response.actually_attended.is_(False), 1), else_=0)),
        )
        .select_from(Response)
        .join(Survey, Response.survey_id == Survey.id)
        .join(Group, Survey.group_id == Group.id)
        .where(
            Group.school_id == school.id,
            Survey.lesson_at >= since,
            Response.answer.is_not(None),
        )
    )).one()
    stuck_calls = (await session.execute(
        select(func.count())
        .select_from(SupportCall)
        .join(Survey, SupportCall.survey_id == Survey.id)
        .join(Group, Survey.group_id == Group.id)
        .where(
            Group.school_id == school.id,
            Survey.lesson_at >= since,
            SupportCall.status.in_((SupportCallStatus.pending, SupportCallStatus.open)),
        )
    )).scalar_one()
    return {
        "school": school,
        "admins": n_admins,
        "groups": n_groups,
        "last_activity": last_activity,
        "responded": total or 0,
        "yes": yes or 0,
        "marked": marked or 0,
        "attended": attended or 0,
        "noshow": noshow or 0,
        "stuck_calls": stuck_calls,
    }


async def all_school_rows(session: AsyncSession, since: datetime) -> list[dict]:
    """Срезы всех школ в порядке school.id — сортировку под свой вывод каждый
    потребитель делает сам."""
    schools = (await session.execute(select(School).order_by(School.id))).scalars().all()
    return [await school_row(session, school, since) for school in schools]


async def active_persons(session: AsyncSession, school_id: int, since: datetime) -> int:
    """Сколько разных учеников школы хоть раз ответили на опрос за окно —
    надстройка дашборда поверх school_row."""
    return (await session.execute(
        select(func.count(func.distinct(Response.person_id)))
        .select_from(Response)
        .join(Survey, Response.survey_id == Survey.id)
        .join(Group, Survey.group_id == Group.id)
        .where(
            Group.school_id == school_id,
            Survey.lesson_at >= since,
            Response.answered_at.is_not(None),
        )
    )).scalar_one()


async def outbox_health(session: AsyncSession) -> dict:
    """Живой снапшот очереди (не windowed — текущее состояние, не агрегат за
    период): сколько строк ждёт отправки и возраст самой старой из них —
    основной сигнал «завис ли sender worker» для Phase 9 алертов."""
    pending, oldest = (await session.execute(
        select(func.count(), func.min(MessageOutbox.scheduled_at))
        .select_from(MessageOutbox)
        .where(MessageOutbox.status == "pending")
    )).one()
    oldest_age_seconds = None
    if oldest is not None:
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        oldest_age_seconds = (datetime.now(UTC) - oldest).total_seconds()
    return {"pending": pending or 0, "oldest_pending_age_seconds": oldest_age_seconds}


async def outbox_send_counts(session: AsyncSession, since: datetime) -> dict:
    """Windowed (тот же WINDOW-конвенция, что у school_row) счётчики отправки.
    3-статусная модель outbox (Phase 3) не хранит retry_after/blocked
    отдельными статусами — `retrying` выведен из текущих pending со
    attempts>0 (живой gauge, не windowed — прошедшие retry не оставляют следа
    после разрешения строки), `blocked` — эвристика по last_error среди
    terminal_failed (Telegram всегда отдаёт текст с «Forbidden» на
    заблокировавшего бота пользователя, см. TelegramForbiddenError)."""
    sent = (await session.execute(
        select(func.count()).select_from(MessageOutbox)
        .where(MessageOutbox.status == "sent", MessageOutbox.sent_at >= since)
    )).scalar_one()
    failed = (await session.execute(
        select(func.count()).select_from(MessageOutbox)
        .where(MessageOutbox.status == "terminal_failed", MessageOutbox.created_at >= since)
    )).scalar_one()
    blocked = (await session.execute(
        select(func.count()).select_from(MessageOutbox)
        .where(
            MessageOutbox.status == "terminal_failed",
            MessageOutbox.created_at >= since,
            MessageOutbox.last_error.ilike("%forbidden%"),
        )
    )).scalar_one()
    retrying = (await session.execute(
        select(func.count()).select_from(MessageOutbox)
        .where(MessageOutbox.status == "pending", MessageOutbox.attempts > 0)
    )).scalar_one()
    return {"sent": sent, "failed": failed, "blocked": blocked, "retrying": retrying}


def session_duration_stats() -> dict:
    """Passthrough к app/db/session.py — count/avg/max секунд на сессию,
    с момента старта процесса."""
    return _session_duration_stats()


def callback_latency_stats() -> dict:
    """Passthrough к app/bot/middlewares.py — count/avg/max секунд обработки
    callback_query, с момента старта процесса."""
    return _callback_latency_stats()


def support_claim_conflict_count() -> int:
    """Passthrough к app/services/supports.py — сколько раз claim() отклонил
    тап из-за реальной гонки (не идемпотентный re-tap тем же человеком), с
    момента старта процесса."""
    return _claim_conflict_count()


def last_tick_stats() -> dict | None:
    """Passthrough к app/core/metrics.py (см. комментарий там про цикл
    scheduler.py↔devstats.py) — длительность и slow-статус последнего
    прогона планировщика. None, если тик ещё ни разу не отработал."""
    return _last_tick.value
