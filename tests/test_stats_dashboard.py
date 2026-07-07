"""Тесты HTML-дашборда статистики (scripts/stats_dashboard.py)."""

from datetime import UTC, datetime

from app.db.models import Answer, Gender, SurveyKind, SurveyStatus
from app.db.session import session_scope
from app.services import admins, groups, persons, surveys
from scripts.stats_dashboard import (
    _cell_code,
    demo_data,
    load_from_db,
    render_html,
)

NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


def test_demo_render_structure() -> None:
    """Демо-дашборд: плитки, карточки групп, оба SVG-чарта, матрица, тултипы."""
    html = render_html(demo_data(), NOW)
    assert "<!doctype html>" in html
    assert "Студия «Ритм» (демо)" in html
    assert html.count('<section class="card">') == 3
    # на карточку — тренд + баланс
    assert html.count("<svg") == 6
    assert 'class="matrix"' in html
    assert "data-tip=" in html  # hover-слой присутствует
    assert "prefers-color-scheme: dark" in html  # тёмная тема выбрана, не флип


def test_cell_codes() -> None:
    assert _cell_code(Answer.yes, True) == "v"
    assert _cell_code(Answer.yes, False) == "x"
    assert _cell_code(Answer.yes, None) == "p"
    assert _cell_code(Answer.no, None) == "n"
    assert _cell_code(None, None) == "s"
    assert _cell_code(None, True) == "w"  # walk-in: пришёл без обещания


async def test_load_from_db_empty(db) -> None:
    schools = await load_from_db()
    html = render_html(schools, NOW)
    assert "Школ пока нет" in html


async def test_load_from_db_with_closed_survey(db) -> None:
    """Живой путь: закрытый опрос с ответами попадает в графики и матрицу."""
    lesson_at = datetime(2026, 7, 1, 19, 0, tzinfo=UTC)
    async with session_scope() as s:
        t = await admins.get_or_create(s, 1, "t", "Препод")
        g = await groups.create_group(s, t.id, "Сальса", "0", "19:00", 18)
        boris, _ = await persons.add_to_group(s, g, 700, "u", "Боря")
        await persons.set_gender(s, boris.id, Gender.male)
        anya, _ = await persons.add_to_group(s, g, 701, "u", "Аня")
        await persons.set_gender(s, anya.id, Gender.female)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, lesson_at
        )
        await surveys.ensure_responses(s, survey.id, [boris.id, anya.id])
        await surveys.record_answer(s, survey.id, boris.id, Answer.yes)
        await surveys.record_answer(s, survey.id, anya.id, Answer.yes)
        survey.status = SurveyStatus.closed

    schools = await load_from_db()
    assert len(schools) == 1
    group = schools[0].groups[0]
    assert len(group.lessons) == 1
    les = group.lessons[0]
    assert (les.promised, les.male_yes, les.female_yes) == (2, 1, 1)
    assert {r.name for r in group.matrix} == {"Боря", "Аня"}

    html = render_html(schools, NOW)
    assert "Сальса" in html and "<svg" in html
