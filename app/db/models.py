from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SurveyKind(enum.StrEnum):
    attendance = "attendance"
    feedback = "feedback"


class SurveyStatus(enum.StrEnum):
    open = "open"
    closed = "closed"


class Answer(enum.StrEnum):
    yes = "yes"
    no = "no"


class Gender(enum.StrEnum):
    male = "male"
    female = "female"


class SupportCallStatus(enum.StrEnum):
    pending = "pending"  # awaiting the admin's confirmation
    open = "open"  # broadcasting / collecting claims
    closed = "closed"  # filled, declined, or nothing was needed


class RecruitMode(enum.StrEnum):
    """Режим автодобора саппортов, личный у каждого препода (lead'а).

    curated — как было: бот предлагает picker, lead сам выбирает кого позвать.
    open_race — открытая гонка: приглашения сразу всему подходящему пулу (lead +
        коллеги), кто первый откликнулся.
    auto_top — бот сам зовёт топ-N по уровню пула (N = дефицит).
    escalate — curated, но если lead не разобрал за ESCALATE_LEAD до занятия,
        бот авто-докидывает open-race."""

    curated = "curated"
    open_race = "open_race"
    auto_top = "auto_top"
    escalate = "escalate"


class SchoolMemberRole(enum.StrEnum):
    """Роли админа внутри школы. Owner — создатель, может всё включая удалить
    школу и убрать co-admin'ов; coadmin — равные операционные права, но не
    может удалить школу или кикнуть owner'а."""

    owner = "owner"
    coadmin = "coadmin"




class Admin(Base):
    """Bot-user who runs groups inside one or more schools.

    Admin сам по себе не привязан к школе — связь через SchoolMember. Один
    Admin может быть владельцем одной собственной школы и co-admin'ом в
    любом числе чужих.

    UI обычно зовёт их «преподаватель» / «препод» (real-life роль), но в
    коде — Admin."""

    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    tg_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(200))
    # Настройки автодобора — личные у каждого препода (его пул, его «когда удобно
    # разбирать»). Используются для групп, где этот Admin — recruit lead.
    support_threshold: Mapped[int] = mapped_column(Integer, default=2)
    support_cutoff_hours: Mapped[int] = mapped_column(Integer, default=3)  # фаза 2, 0=выкл
    recruit_evening_time: Mapped[str] = mapped_column(String(5), default="20:00")  # фаза 1
    # Режим автодобора (B16) — личный у препода. Дефолт curated = прежнее поведение.
    recruit_mode: Mapped[str] = mapped_column(
        String(16), default=RecruitMode.curated.value
    )
    # Персональная переиспользуемая ссылка-приглашение саппорта (start=support_<code>).
    support_code: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    # True если человек хоть раз прошёл onboarding (создал свою школу либо
    # принял partner-приглашение). Используется чтобы не показывать
    # CreateSchool-prompt бывшим co-admin'ам, которых сняли со всех школ.
    has_completed_setup: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class School(Base):
    """Top-level container: владеет группами, учениками, площадками, пулом
    саппортов. Любой member школы видит весь её контент."""

    __tablename__ = "schools"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    # IANA timezone name (Europe/Moscow, America/New_York, …). Все lesson_at
    # хранятся как aware UTC; конвертация в этот TZ — при отображении и при
    # расчёте «занятие в Пн в 19:00 местного времени».
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SchoolMember(Base):
    """Admin-в-школе. Уникален по (school_id, admin_id)."""

    __tablename__ = "school_members"
    __table_args__ = (
        UniqueConstraint("school_id", "admin_id", name="uq_school_members"),
        # admin_id-led lookups (admin_schools/school_role) filter further by role;
        # school_id trails for the rarer school-scoped-by-role queries.
        Index("ix_school_members_admin_role_school", "admin_id", "role", "school_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    school_id: Mapped[int] = mapped_column(ForeignKey("schools.id", ondelete="CASCADE"))
    admin_id: Mapped[int] = mapped_column(ForeignKey("admins.id", ondelete="CASCADE"))
    role: Mapped[SchoolMemberRole] = mapped_column(
        Enum(SchoolMemberRole, name="school_member_role", native_enum=False),
        default=SchoolMemberRole.coadmin,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SchoolInvite(Base):
    """Одноразовая ссылка-приглашение в школу. Принятие → SchoolMember(coadmin)."""

    __tablename__ = "school_invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    school_id: Mapped[int] = mapped_column(ForeignKey("schools.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(32), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Venue(Base):
    """Place a school's lessons happen — reused across groups in that school."""

    __tablename__ = "venues"

    id: Mapped[int] = mapped_column(primary_key=True)
    school_id: Mapped[int | None] = mapped_column(
        ForeignKey("schools.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(120))
    address: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TeacherPreset(Base):
    """Сохранённая связка «кто ведёт» школы («Маша + Петя») — кнопки-подсказки
    на шаге преподавателей в мастере группы, редактируются в меню школы."""

    __tablename__ = "teacher_presets"

    id: Mapped[int] = mapped_column(primary_key=True)
    school_id: Mapped[int] = mapped_column(
        ForeignKey("schools.id", ondelete="CASCADE")
    )
    note: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Group(Base):
    __tablename__ = "groups"

    __table_args__ = (
        # Grepped app/services/groups.py: active_groups() filters on `active`
        # alone (no school_id in the SQL WHERE anywhere in the codebase) — a
        # school_id-composite index would just be dead weight on the second
        # column, so single-column instead of the originally-proposed
        # (active, school_id) composite. See Phase 2 EXPLAIN ANALYZE notes.
        Index("ix_groups_active", "active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    school_id: Mapped[int | None] = mapped_column(
        ForeignKey("schools.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(200))  # auto-generated from style+level
    style: Mapped[str | None] = mapped_column(String(80), nullable=True)
    level: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0..4
    weekdays: Mapped[str] = mapped_column(String(20))  # "0,2" — Mon,Wed (0=Mon)
    lesson_time: Mapped[str] = mapped_column(String(5))  # "19:00"
    duration_min: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 60/90/120/…
    lead_hours: Mapped[int] = mapped_column(Integer, default=18)
    venue_id: Mapped[int | None] = mapped_column(
        ForeignKey("venues.id", ondelete="SET NULL"), nullable=True
    )
    room: Mapped[str | None] = mapped_column(String(80), nullable=True)
    teachers_note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    join_code: Mapped[str] = mapped_column(String(32), unique=True)
    # «Куратор добора» — чей пул/настройки/время использует автодобор этой группы.
    # GroupAdmin (несколько) — для сводок/пингов; lead (один) — для recruiting.
    recruit_lead_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LessonCancellation(Base):
    """Отмена одного конкретного занятия. Когда такая запись есть, scheduler не
    создаёт survey и не делает recruit для этой группы на эту дату."""

    __tablename__ = "lesson_cancellations"
    __table_args__ = (
        UniqueConstraint("group_id", "lesson_at", name="uq_cancellation_group_lesson"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    lesson_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LessonPlaceOverride(Base):
    """Другое место/зал для одного конкретного занятия (школы, гоняющие группы
    каждый раз по разным залам). Когда запись есть, все сообщения этого занятия
    (опрос, напоминания, инвайты саппортам) показывают её вместо места группы."""

    __tablename__ = "lesson_place_overrides"
    __table_args__ = (
        UniqueConstraint("group_id", "lesson_at", name="uq_place_override_group_lesson"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    lesson_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    place: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Person(Base):
    """A human inside one school — student and/or support. One row per
    (school, telegram account); group membership через Membership."""

    __tablename__ = "persons"
    __table_args__ = (
        UniqueConstraint("school_id", "tg_user_id", name="uq_persons_school_tg"),
        # find_by_tg/find_all_by_tg look up by tg_user_id across all schools — the
        # composite unique above doesn't help a school-less lookup.
        Index("ix_persons_tg_user_id", "tg_user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    school_id: Mapped[int | None] = mapped_column(
        ForeignKey("schools.id", ondelete="CASCADE"), nullable=True
    )
    tg_user_id: Mapped[int] = mapped_column(BigInteger)
    tg_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(200))
    gender: Mapped[Gender | None] = mapped_column(
        Enum(Gender, name="gender", native_enum=False), nullable=True
    )
    # support_level переехал в SupportPoolEntry (per-teacher pool).
    helped_count: Mapped[int] = mapped_column(Integer, default=0)
    # B5: когда последний раз слали реактивационный пинг (throttle от спама).
    # Naive UTC. None — ещё ни разу.
    last_reactivation_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Membership(Base):
    """Links a person to a group they regularly attend (many-to-many)."""

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("person_id", "group_id", name="uq_memberships_person_group"),
        # group_members looks up by group_id alone — the composite unique above
        # (person_id-led) doesn't help the group-side lookup.
        Index("ix_memberships_group_id", "group_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GroupAdmin(Base):
    """Ответственный за группу admin. Маршрутизация админ-уведомлений группы
    (picker автодобора, авто-сводка, stale/pending пинги) идёт на этих людей.
    Если у группы нет ни одной записи — fallback на всех членов школы. Видимость
    групп этим НЕ ограничивается — только уведомления."""

    __tablename__ = "group_admins"
    __table_args__ = (
        UniqueConstraint("group_id", "admin_id", name="uq_group_admins"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    admin_id: Mapped[int] = mapped_column(ForeignKey("admins.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SupportPoolEntry(Base):
    """Персональный пул саппортов препода: (admin, person) → level. Один человек
    может быть в пулах нескольких преподов с разными уровнями. Уровень = приоритет
    в picker'е (3 первым). Раньше был один `Person.support_level` на всю школу."""

    __tablename__ = "support_pool_entries"
    __table_args__ = (
        UniqueConstraint("admin_id", "person_id", name="uq_support_pool"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int] = mapped_column(ForeignKey("admins.id", ondelete="CASCADE"))
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    level: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Survey(Base):
    __tablename__ = "surveys"
    __table_args__ = (
        UniqueConstraint("group_id", "kind", "lesson_at", name="uq_surveys_group_kind_lesson"),
        # close_past_surveys scans open surveys past their lesson time every tick.
        Index("ix_surveys_status_lesson_at", "status", "lesson_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    kind: Mapped[SurveyKind] = mapped_column(
        Enum(SurveyKind, name="survey_kind", native_enum=False)
    )
    lesson_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[SurveyStatus] = mapped_column(
        Enum(SurveyStatus, name="survey_status", native_enum=False), default=SurveyStatus.open
    )
    summary_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Response(Base):
    __tablename__ = "responses"
    __table_args__ = (
        UniqueConstraint("survey_id", "person_id", name="uq_responses_survey_person"),
        # person_id-led lookups (drop responses for a leaving member) aren't
        # covered by the survey_id-led unique constraint above.
        Index("ix_responses_person_survey", "person_id", "survey_id"),
        # answer-filtered aggregates per survey (summary/devstats).
        Index("ix_responses_survey_answer", "survey_id", "answer"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    survey_id: Mapped[int] = mapped_column(ForeignKey("surveys.id", ondelete="CASCADE"))
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    answer: Mapped[Answer | None] = mapped_column(
        Enum(Answer, name="answer", native_enum=False), nullable=True
    )
    # «Реально пришёл?» — заполняется preподом после занятия. None = не отмечено.
    actually_attended: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Auto-reminder флаги: scheduler ставит True после отправки напоминания
    # «придёшь?» в соответствующем окне. Идемпотентность ретраев.
    reminded_4h: Mapped[bool] = mapped_column(Boolean, default=False)
    reminded_1h: Mapped[bool] = mapped_column(Boolean, default=False)


class SupportCall(Base):
    """A drive to recruit supports for one lesson's gender imbalance."""

    __tablename__ = "support_calls"
    __table_args__ = (
        UniqueConstraint("survey_id", name="uq_support_calls_survey"),
        # Scheduler scans pending_unnotified_calls/no_reply_calls every tick,
        # filtering by status then a created_at range — created_at trails as the
        # column with the widest range predicate.
        Index("ix_support_calls_status_created_at", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    survey_id: Mapped[int] = mapped_column(ForeignKey("surveys.id", ondelete="CASCADE"))
    gender: Mapped[Gender] = mapped_column(
        Enum(Gender, name="support_call_gender", native_enum=False)
    )
    needed: Mapped[int] = mapped_column(Integer)
    status: Mapped[SupportCallStatus] = mapped_column(
        Enum(SupportCallStatus, name="support_call_status", native_enum=False),
        default=SupportCallStatus.pending,
    )
    # Помечается, когда scheduler отправил «никто не откликнулся за X часов» —
    # чтобы не пинговать препода повторно на каждом tick'е.
    no_reply_notified: Mapped[bool] = mapped_column(Boolean, default=False)
    # Owner уже получил напоминание «разбери добор» по зависшему pending-call'у.
    pending_notified: Mapped[bool] = mapped_column(Boolean, default=False)
    # Фаза 2 уже отрабатывала на этом занятии (добор остатка делается один раз).
    topup_done: Mapped[bool] = mapped_column(Boolean, default=False)
    # B8: ссылка на recruit-сообщение lead'а (picker/heads-up) с баланс-строкой —
    # чтобы обновлять баланс в реальном времени при claim/skip. None — не сохранено.
    picker_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    picker_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Сообщение из picker_ref несёт живую inline-клавиатуру (picker с кнопками
    # выбора саппортов), а не просто текст-хедсап — редактировать его текст без
    # reply_markup нельзя, иначе кнопки пропадают (см. _refresh_balance).
    picker_has_keyboard: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SupportClaim(Base):
    """One person agreeing to support a lesson via a SupportCall."""

    __tablename__ = "support_claims"
    __table_args__ = (
        UniqueConstraint("support_call_id", "person_id", name="uq_support_claims"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    support_call_id: Mapped[int] = mapped_column(
        ForeignKey("support_calls.id", ondelete="CASCADE")
    )
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    # «Реально пришёл выручить?» — отмечается preподом после занятия. None = не
    # отмечено. helped_count считается по числу claim'ов с attended=True.
    attended: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MessageOutbox(Base):
    """Durable send queue — every outbound Telegram message goes through here
    instead of a direct `bot.send_message` in a loop, so a crash mid-fan-out
    doesn't silently lose messages and a single slow/rate-limited recipient
    doesn't block the rest of the batch.

    `status` is plain VARCHAR (not native_enum), values: `pending` (never
    attempted, or scheduled for a retry), `sent`, `terminal_failed` (gave up
    after `app.services.outbox.MAX_ATTEMPTS`). No separate transient
    "in-flight" status: a worker claims a row via `SELECT ... FOR UPDATE SKIP
    LOCKED` and holds that row lock for the duration of the send attempt, so
    the row is never externally visible mid-attempt — it resolves straight
    from `pending` to `sent`/`terminal_failed`, or back to `pending` with a
    pushed-forward `scheduled_at` for a retry."""

    __tablename__ = "message_outbox"
    __table_args__ = (
        UniqueConstraint("business_key", name="uq_message_outbox_business_key"),
        # Dequeue query: WHERE status='pending' AND scheduled_at<=now()
        # ORDER BY priority DESC, scheduled_at.
        Index("ix_message_outbox_dequeue", "status", "priority", "scheduled_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    business_key: Mapped[str] = mapped_column(String(200))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    payload_json: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Nullable — только broadcast-каскады (Phase 5) заполняют его. Прямой FK
    # избавляет progress-запрос от парсинга business_key строк.
    broadcast_id: Mapped[int | None] = mapped_column(
        ForeignKey("broadcasts.id", ondelete="CASCADE"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Broadcast(Base):
    """One broadcast run — the parent record. Per-recipient delivery state
    lives in `message_outbox` rows (`broadcast_id` FK), not duplicated here.

    `scope_type`/`scope_payload_json` are deliberately general (not a plain
    `group_id` column) so Phase 11's global developer broadcast (scope: all
    schools / one school / a subset; audience: all Person / Admin-only) reuses
    this exact table without a schema migration — Phase 5 itself only ever
    writes `scope_type="group"`."""

    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(20))
    scope_payload_json: Mapped[dict] = mapped_column(JSONB)
    audience_type: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    created_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admins.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FSMState(Base):
    """Postgres-backed aiogram FSM storage (Phase 6, replaces JsonFileStorage —
    a whole-file rewrite on every dialog transition serialized the entire
    event loop under concurrent onboarding). `key` is the same composite
    string `app/bot/fsm_storage.py` always built (bot_id:chat_id:user_id:
    thread_id:business_connection_id:destiny) — `destiny` MUST stay part of
    it: aiogram-dialog's stack vs. per-intent context collide into the same
    key without it (this project hit a real `KeyError: 'state'` from exactly
    that mistake once). `data` holds `{"state": ..., "data": {...}}`, mirroring
    the old JSON file's per-key slot shape."""

    __tablename__ = "fsm_state"
    __table_args__ = (
        # TTL cleanup scans WHERE updated_at < threshold.
        Index("ix_fsm_state_updated_at", "updated_at"),
    )

    key: Mapped[str] = mapped_column(String, primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DevUiPref(Base):
    """Phase 10: какой UI разработчик видит на /start — `developer` (dev-меню
    +дашборд) или `teacher` (проходит ровно тот же путь, что настоящий
    препод/новый юзер — дожфудинг). НЕ привязана к Admin (`tg_user_id`, не
    FK) — разработчику необязательно иметь собственную школу, чтобы
    пользоваться dev-режимом."""

    __tablename__ = "dev_ui_pref"

    tg_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ui_mode: Mapped[str] = mapped_column(String(20), default="developer")
