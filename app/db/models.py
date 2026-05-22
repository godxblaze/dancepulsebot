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
    pending = "pending"  # awaiting the teacher's confirmation
    open = "open"  # broadcasting / collecting claims
    closed = "closed"  # filled, declined, or nothing was needed


class Teacher(Base):
    __tablename__ = "teachers"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    tg_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(200))
    support_threshold: Mapped[int] = mapped_column(Integer, default=2)
    support_cutoff_hours: Mapped[int] = mapped_column(Integer, default=6)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("teachers.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    weekdays: Mapped[str] = mapped_column(String(20))  # "0,2" — Mon,Wed (0=Mon)
    lesson_time: Mapped[str] = mapped_column(String(5))  # "19:00"
    lead_hours: Mapped[int] = mapped_column(Integer, default=18)
    join_code: Mapped[str] = mapped_column(String(32), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Person(Base):
    """A human in a teacher's audience — a student of groups and/or a support.

    One row per (teacher, telegram account); group membership lives in Membership.
    """

    __tablename__ = "persons"
    __table_args__ = (
        UniqueConstraint("teacher_id", "tg_user_id", name="uq_persons_teacher_tg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("teachers.id", ondelete="CASCADE"))
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
