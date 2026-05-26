from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
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
    support_threshold: Mapped[int] = mapped_column(Integer, default=2)
    support_cutoff_hours: Mapped[int] = mapped_column(Integer, default=6)
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SchoolMember(Base):
    """Admin-в-школе. Уникален по (school_id, admin_id)."""

    __tablename__ = "school_members"
    __table_args__ = (
        UniqueConstraint("school_id", "admin_id", name="uq_school_members"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    school_id: Mapped[int] = mapped_column(ForeignKey("schools.id", ondelete="CASCADE"))
    admin_id: Mapped[int] = mapped_column(ForeignKey("admins.id", ondelete="CASCADE"))
    role: Mapped[SchoolMemberRole] = mapped_column(
        Enum(SchoolMemberRole, name="school_member_role"),
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
    # legacy: admin_id остался от прошлой модели. Удалить вместе с цепочкой
    # на этапе 5; пока nullable, новый код пишет только school_id.
    admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admins.id", ondelete="CASCADE"), nullable=True
    )
    school_id: Mapped[int | None] = mapped_column(
        ForeignKey("schools.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(120))
    address: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    # legacy admin_id оставлен на время рефактора. Новый код использует school_id.
    admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admins.id", ondelete="CASCADE"), nullable=True
    )
    school_id: Mapped[int | None] = mapped_column(
        ForeignKey("schools.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(200))  # auto-generated from style+level
    style: Mapped[str | None] = mapped_column(String(80), nullable=True)
    level: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0..4
    weekdays: Mapped[str] = mapped_column(String(20))  # "0,2" — Mon,Wed (0=Mon)
    lesson_time: Mapped[str] = mapped_column(String(5))  # "19:00"
    lead_hours: Mapped[int] = mapped_column(Integer, default=18)
    venue_id: Mapped[int | None] = mapped_column(
        ForeignKey("venues.id", ondelete="SET NULL"), nullable=True
    )
    room: Mapped[str | None] = mapped_column(String(80), nullable=True)
    teachers_note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    join_code: Mapped[str] = mapped_column(String(32), unique=True)
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
    lesson_at: Mapped[datetime] = mapped_column(DateTime)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Person(Base):
    """A human inside one school — student and/or support. One row per
    (school, telegram account); group membership через Membership."""

    __tablename__ = "persons"
    __table_args__ = (
        # legacy unique для совместимости со старой схемой; новый — на school_id.
        UniqueConstraint("admin_id", "tg_user_id", name="uq_persons_admin_tg"),
        UniqueConstraint("school_id", "tg_user_id", name="uq_persons_school_tg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admins.id", ondelete="CASCADE"), nullable=True
    )
    school_id: Mapped[int | None] = mapped_column(
        ForeignKey("schools.id", ondelete="CASCADE"), nullable=True
    )
    tg_user_id: Mapped[int] = mapped_column(BigInteger)
    tg_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(200))
    gender: Mapped[Gender | None] = mapped_column(
        Enum(Gender, name="gender"), nullable=True
    )
    support_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    helped_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Membership(Base):
    """Links a person to a group they regularly attend (many-to-many)."""

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("person_id", "group_id", name="uq_memberships_person_group"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Survey(Base):
    __tablename__ = "surveys"
    __table_args__ = (
        UniqueConstraint("group_id", "kind", "lesson_at", name="uq_surveys_group_kind_lesson"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    kind: Mapped[SurveyKind] = mapped_column(Enum(SurveyKind, name="survey_kind"))
    lesson_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[SurveyStatus] = mapped_column(
        Enum(SurveyStatus, name="survey_status"), default=SurveyStatus.open
    )
    summary_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Response(Base):
    __tablename__ = "responses"
    __table_args__ = (
        UniqueConstraint("survey_id", "person_id", name="uq_responses_survey_person"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    survey_id: Mapped[int] = mapped_column(ForeignKey("surveys.id", ondelete="CASCADE"))
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    answer: Mapped[Answer | None] = mapped_column(Enum(Answer, name="answer"), nullable=True)
    # «Реально пришёл?» — заполняется preподом после занятия. None = не отмечено.
    actually_attended: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SupportCall(Base):
    """A drive to recruit supports for one lesson's gender imbalance."""

    __tablename__ = "support_calls"
    __table_args__ = (UniqueConstraint("survey_id", name="uq_support_calls_survey"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    survey_id: Mapped[int] = mapped_column(ForeignKey("surveys.id", ondelete="CASCADE"))
    gender: Mapped[Gender] = mapped_column(Enum(Gender, name="support_call_gender"))
    needed: Mapped[int] = mapped_column(Integer)
    status: Mapped[SupportCallStatus] = mapped_column(
        Enum(SupportCallStatus, name="support_call_status"),
        default=SupportCallStatus.pending,
    )
    # Помечается, когда scheduler отправил «никто не откликнулся за X часов» —
    # чтобы не пинговать препода повторно на каждом tick'е.
    no_reply_notified: Mapped[bool] = mapped_column(Boolean, default=False)
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
