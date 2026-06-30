"""Shared scaffolding for the handler-driven e2e suites — same FakeBot/SQLite
approach as test_e2e_scenarios.py, factored out so the school / attendance /
admin-misc files don't each re-declare group/join helpers."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.bot.handlers.student import join_gender, join_name_typed, join_start
from app.bot.states import JoinFlow
from app.db.models import Gender
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import groups as groups_svc
from tests._fakes import FakeBot, FakeCallback, FakeFSM, FakeMessage, SentMessage, fake_user

_MSK = ZoneInfo("Europe/Moscow")
LESSON = datetime(2026, 6, 1, 19, 0, tzinfo=_MSK).astimezone(UTC)  # Monday 19:00
TEACHER_TG = 100


async def make_group(
    *,
    teacher_tg: int = TEACHER_TG,
    teacher_name: str = "Препод",
    style: str = "Бачата",
    weekday: int = 0,
    time: str = "19:00",
) -> tuple[int, int, str]:
    """Create teacher + owned school + one group. Returns (group_id, school_id, join_code)."""
    async with session_scope() as s:
        await admins_svc.get_or_create(s, teacher_tg, None, teacher_name)
        t = await admins_svc.get_by_tg_id(s, teacher_tg)
        group = await groups_svc.create_group(
            s, t.id, style, str(weekday), time, 18, style=style,
        )
        g = await groups_svc.get_group(s, group.id)
        return g.id, g.school_id, g.join_code


async def join_via_link(
    bot: FakeBot, code: str, tg_id: int, name: str, gender: Gender
) -> None:
    """Drive the join FSM: deep-link → typed name → gender pick."""
    user = fake_user(tg_id, full_name=name)
    fsm = FakeFSM()
    start_msg = FakeMessage(user, bot=bot)
    await join_start(start_msg, SimpleNamespace(args=f"join_{code}"), fsm, bot)
    assert fsm.state == JoinFlow.name
    await join_name_typed(FakeMessage(user, text=name, bot=bot), fsm)
    assert fsm.state == JoinFlow.gender
    gender_cb = FakeCallback(
        f"join:gender:{'male' if gender == Gender.male else 'female'}",
        user,
        message=SentMessage(None, tg_id, ""),
    )
    await join_gender(gender_cb, fsm, bot)
    assert fsm.state is None


def cb(data: str, tg_id: int, *, name: str = "Препод", message: SentMessage | None = None,
       bot: FakeBot | None = None) -> FakeCallback:
    """Shorthand for a callback from `tg_id`, carrying a fresh SentMessage if none given."""
    msg = message if message is not None else SentMessage(bot, tg_id, "")
    return FakeCallback(data, fake_user(tg_id, full_name=name), message=msg)
