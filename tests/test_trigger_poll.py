"""scripts/trigger_poll.py — пункт #20 аудита (по заметкам аудита скрипт был
сломан, без единой регрессии). Проверяет ветвление run() (нет групп/без
weekdays/занятие отменено/опрос уже разослан/группа реально требует рассылки)
без похода в живой Telegram — build_bot/init_db/setup_logging и сама рассылка
(_poll_group) заменены фейками."""
import scripts.trigger_poll as trigger_poll
from app.db.session import session_scope
from app.services import admins, cancellations, groups, surveys
from app.services.schedule import next_lesson_at, utc_now

TEACHER_TG = 900


class _FakeBotSession:
    async def close(self) -> None:
        pass


class _FakeBot:
    def __init__(self) -> None:
        self.session = _FakeBotSession()


def _patch_bot_and_infra(monkeypatch) -> None:
    monkeypatch.setattr(trigger_poll, "build_bot", lambda: _FakeBot())
    monkeypatch.setattr(trigger_poll, "init_db", _noop)
    monkeypatch.setattr(trigger_poll, "setup_logging", lambda: None)


async def _noop() -> None:
    pass


async def _make_group(weekdays: str = "0", lesson_time: str = "19:00"):
    async with session_scope() as s:
        t = await admins.get_or_create(s, TEACHER_TG, "t", "Препод")
        g = await groups.create_group(s, t.id, "Бачата", weekdays, lesson_time, 18)
        return g.id


async def test_run_no_active_groups_skips_poll(monkeypatch, capsys, db) -> None:
    _patch_bot_and_infra(monkeypatch)
    polled = []
    monkeypatch.setattr(trigger_poll, "_poll_group", lambda *a, **kw: polled.append(a))
    await trigger_poll.run()
    assert polled == []
    assert "Нет активных групп" in capsys.readouterr().out


async def test_run_skips_group_without_weekdays(monkeypatch, capsys, db) -> None:
    await _make_group(weekdays="")  # без дней недели — next_lesson_at вернёт None
    _patch_bot_and_infra(monkeypatch)
    polled = []
    monkeypatch.setattr(trigger_poll, "_poll_group", lambda *a, **kw: polled.append(a))
    await trigger_poll.run()
    assert polled == []
    assert "не заданы дни недели" in capsys.readouterr().out


async def test_run_skips_cancelled_lesson(monkeypatch, capsys, db) -> None:
    gid = await _make_group()
    now = utc_now()
    lesson_at = next_lesson_at("0", "19:00", now, tz=None)
    async with session_scope() as s:
        await cancellations.cancel(s, gid, lesson_at)
    _patch_bot_and_infra(monkeypatch)
    polled = []
    monkeypatch.setattr(trigger_poll, "_poll_group", lambda *a, **kw: polled.append(a))
    await trigger_poll.run()
    assert polled == []
    assert "отменено" in capsys.readouterr().out


async def test_run_skips_group_with_existing_survey(monkeypatch, capsys, db) -> None:
    from app.db.models import SurveyKind

    gid = await _make_group()
    now = utc_now()
    lesson_at = next_lesson_at("0", "19:00", now, tz=None)
    async with session_scope() as s:
        await surveys.get_or_create_survey(s, gid, SurveyKind.attendance, lesson_at)
    _patch_bot_and_infra(monkeypatch)
    polled = []
    monkeypatch.setattr(trigger_poll, "_poll_group", lambda *a, **kw: polled.append(a))
    await trigger_poll.run()
    assert polled == []
    assert "уже разослан" in capsys.readouterr().out


async def test_run_polls_eligible_group(monkeypatch, capsys, db) -> None:
    gid = await _make_group()
    _patch_bot_and_infra(monkeypatch)
    calls = []

    async def _fake_poll_group(bot, group, lesson_at, now, *, tz=None):
        calls.append((bot, group.id, lesson_at, tz))

    monkeypatch.setattr(trigger_poll, "_poll_group", _fake_poll_group)
    await trigger_poll.run()
    assert len(calls) == 1
    assert calls[0][1] == gid
    out = capsys.readouterr().out
    assert "рассылаю опрос" in out
    assert "Готово." in out
