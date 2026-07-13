"""scripts/check_pilot.py — пункт #20 аудита (модуль без тестов). Проверяет
каждый из 5 детекторов префлайта по отдельности плюс «чистое» состояние, где
ни один не срабатывает. init_db() заменён no-op — схема уже поднята conftest'ом,
повторный alembic upgrade в тесте не нужен и опасен (целится не в ту схему)."""
from datetime import UTC, datetime, timedelta

import scripts.check_pilot as check_pilot
from app.db.models import Answer, Gender, SurveyKind
from app.db.session import session_scope
from app.services import admins, groups, persons, schools, surveys


async def _noop() -> None:
    pass


def _patch_init_db(monkeypatch) -> None:
    monkeypatch.setattr(check_pilot, "init_db", _noop)


async def _admin_with_school(tg_id: int) -> tuple[int, int]:
    async with session_scope() as s:
        t = await admins.get_or_create(s, tg_id, "t", "Препод")
        return t.id, t.id  # owned-школа тестового мока — id совпадает с admin.id


async def test_check_pilot_clean_state_reports_no_issues(monkeypatch, capsys, db) -> None:
    admin_id, sid = await _admin_with_school(910)
    async with session_scope() as s:
        g = await groups.create_group(
            s, sid, "Бачата", "0", "19:00", 18, created_by_admin_id=admin_id
        )
        p, _ = await persons.add_to_group(s, g, 911, "u", "Ученик")
        await persons.set_gender(s, p.id, Gender.female)
    _patch_init_db(monkeypatch)
    await check_pilot.run()
    out = capsys.readouterr().out
    assert "чист" in out
    assert "⚠️" not in out


async def test_check_pilot_detects_null_gender_in_pool(monkeypatch, capsys, db) -> None:
    admin_id, sid = await _admin_with_school(920)
    async with session_scope() as s:
        g = await groups.create_group(
            s, sid, "Бачата", "0", "19:00", 18, created_by_admin_id=admin_id
        )
        p, _ = await persons.add_to_group(s, g, 921, "u", "Саппорт")
        await persons.set_pool_level(s, admin_id, p.id, 1)
    _patch_init_db(monkeypatch)
    await check_pilot.run()
    out = capsys.readouterr().out
    assert "БЕЗ пола" in out
    assert "не попадут в добор" in out


async def test_check_pilot_detects_null_gender_yes_on_open_survey(
    monkeypatch, capsys, db
) -> None:
    admin_id, sid = await _admin_with_school(930)
    async with session_scope() as s:
        g = await groups.create_group(
            s, sid, "Бачата", "0", "19:00", 18, created_by_admin_id=admin_id
        )
        p, _ = await persons.add_to_group(s, g, 931, "u", "Ученик")
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, datetime.now(UTC) + timedelta(days=7)
        )
        await surveys.record_answer(s, survey.id, p.id, Answer.yes)
    _patch_init_db(monkeypatch)
    await check_pilot.run()
    out = capsys.readouterr().out
    assert "ответили «приду» БЕЗ пола" in out


async def test_check_pilot_detects_active_group_without_weekdays(
    monkeypatch, capsys, db
) -> None:
    admin_id, sid = await _admin_with_school(940)
    async with session_scope() as s:
        await groups.create_group(
            s, sid, "Бачата", "", "19:00", 18, created_by_admin_id=admin_id
        )
    _patch_init_db(monkeypatch)
    await check_pilot.run()
    out = capsys.readouterr().out
    assert "без дней недели" in out


async def test_check_pilot_group_without_lead_is_info_only(monkeypatch, capsys, db) -> None:
    """Отсутствие куратора добора — информационная строка (ℹ️), не должна
    считаться «проблемой» и портить итоговый чистый вердикт."""
    _admin_id, sid = await _admin_with_school(950)
    async with session_scope() as s:
        await groups.create_group(s, sid, "Бачата", "0", "19:00", 18)
    _patch_init_db(monkeypatch)
    await check_pilot.run()
    out = capsys.readouterr().out
    assert "без явного куратора добора" in out
    assert "чист" in out  # не влияет на issues


async def test_check_pilot_detects_invalid_school_timezone(monkeypatch, capsys, db) -> None:
    _admin_id, sid = await _admin_with_school(960)
    async with session_scope() as s:
        await schools.set_timezone(s, sid, "Not/ARealZone")
    _patch_init_db(monkeypatch)
    await check_pilot.run()
    out = capsys.readouterr().out
    assert "невалидный timezone" in out
