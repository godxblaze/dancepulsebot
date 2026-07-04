"""Integration test of the core attendance-survey flow."""
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.db.models import Answer, SurveyKind
from app.db.session import session_scope
from app.services import admins, groups, persons, surveys

_MSK = ZoneInfo("Europe/Moscow")
LESSON = datetime(2026, 5, 25, 19, 0, tzinfo=_MSK).astimezone(UTC)


async def _setup_group() -> tuple[int, list[int]]:
    async with session_scope() as s:
        teacher = await admins.get_or_create(s, 111, "t", "Препод")
        group = await groups.create_group(s, teacher.id, "Группа A", "0,2", "19:00", 18)
        p1, _ = await persons.add_to_group(s, group, 201, "a", "Аня")
        p2, _ = await persons.add_to_group(s, group, 202, "b", "Боря")
        p3, _ = await persons.add_to_group(s, group, 203, "c", "Вика")
        return group.id, [p1.id, p2.id, p3.id]


async def test_survey_flow(db) -> None:
    group_id, person_ids = await _setup_group()

    # create survey + empty responses
    async with session_scope() as s:
        survey, created = await surveys.get_or_create_survey(
            s, group_id, SurveyKind.attendance, LESSON
        )
        assert created
        await surveys.ensure_responses(s, survey.id, person_ids)
        survey_id = survey.id

    # idempotency — second call must not re-create
    async with session_scope() as s:
        _, created_again = await surveys.get_or_create_survey(
            s, group_id, SurveyKind.attendance, LESSON
        )
        assert not created_again

    # two persons answer, one stays silent
    async with session_scope() as s:
        await surveys.record_answer(s, survey_id, person_ids[0], Answer.yes)
        await surveys.record_answer(s, survey_id, person_ids[1], Answer.no)

    async with session_scope() as s:
        summary = await surveys.summarize(s, survey_id)
    assert (summary.total, summary.yes, summary.no, summary.pending) == (3, 1, 1, 1)
    assert summary.no_names == ["Боря"]
    assert summary.pending_names == ["Вика"]


async def test_answer_can_be_changed(db) -> None:
    group_id, person_ids = await _setup_group()
    async with session_scope() as s:
        survey, _ = await surveys.get_or_create_survey(
            s, group_id, SurveyKind.attendance, LESSON
        )
        await surveys.ensure_responses(s, survey.id, person_ids)
        survey_id = survey.id

    async with session_scope() as s:
        await surveys.record_answer(s, survey_id, person_ids[0], Answer.yes)
    async with session_scope() as s:
        await surveys.record_answer(s, survey_id, person_ids[0], Answer.no)

    async with session_scope() as s:
        summary = await surveys.summarize(s, survey_id)
    assert (summary.yes, summary.no) == (0, 1)


async def test_close_past_surveys(db) -> None:
    group_id, _ = await _setup_group()
    async with session_scope() as s:
        await surveys.get_or_create_survey(s, group_id, SurveyKind.attendance, LESSON)

    async with session_scope() as s:
        closed = await surveys.close_past_surveys(s, datetime(2026, 5, 26, 0, 0))
    assert closed == 1


async def test_recent_closed_surveys(db) -> None:
    group_id, _ = await _setup_group()
    dates = [datetime(2026, 5, d, 19, 0) for d in (11, 13, 18, 20, 25, 27)]
    async with session_scope() as s:
        for d in dates:
            await surveys.get_or_create_survey(s, group_id, SurveyKind.attendance, d)
    async with session_scope() as s:
        await surveys.close_past_surveys(s, datetime(2026, 5, 26, 0, 0))

    async with session_scope() as s:
        recent = await surveys.recent_closed_surveys(s, group_id, limit=3)
    # 5 closed (11..25), newest first, capped at 3
    assert [r.lesson_at.day for r in recent] == [25, 20, 18]


async def test_mark_summary_sent(db) -> None:
    group_id, _ = await _setup_group()
    async with session_scope() as s:
        survey, _ = await surveys.get_or_create_survey(
            s, group_id, SurveyKind.attendance, LESSON
        )
        assert survey.summary_sent is False
        survey_id = survey.id

    async with session_scope() as s:
        await surveys.mark_summary_sent(s, survey_id)
    async with session_scope() as s:
        survey = await surveys.get_survey(s, survey_id)
        assert survey is not None and survey.summary_sent is True


async def test_unknown_yes_count(db) -> None:
    """Ответившие «приду» без пола считаются отдельно и НЕ попадают в
    gender_yes_counts — баланс добора их не видит."""
    from app.db.models import Gender

    group_id, person_ids = await _setup_group()
    async with session_scope() as s:
        survey, _ = await surveys.get_or_create_survey(
            s, group_id, SurveyKind.attendance, LESSON
        )
        survey_id = survey.id
        # Аня — девушка, Боря — парень, Вика — без пола; все «приду».
        await persons.set_gender(s, person_ids[0], Gender.female)
        await persons.set_gender(s, person_ids[1], Gender.male)
        for pid in person_ids:
            await surveys.record_answer(s, survey_id, pid, Answer.yes)

    async with session_scope() as s:
        male, female = await surveys.gender_yes_counts(s, survey_id)
        unknown = await surveys.unknown_yes_count(s, survey_id)
    assert (male, female) == (1, 1)  # Вика без пола не учтена
    assert unknown == 1


async def test_undelivered_not_counted_as_pending(db) -> None:
    """tg_message_id=-1 (заблокировали бота/не нажали Start) — не «молчит»:
    отдельный счётчик undelivered, не в pending/pending_names, pending_persons
    его тоже не возвращает (пинговать недостижимого некому)."""
    group_id, person_ids = await _setup_group()
    async with session_scope() as s:
        survey, _ = await surveys.get_or_create_survey(
            s, group_id, SurveyKind.attendance, LESSON
        )
        await surveys.ensure_responses(s, survey.id, person_ids)
        survey_id = survey.id
        # Вика недостижима; Аня и Боря молчат (не отвечали).
        await surveys.set_message_id(s, survey_id, person_ids[2], -1)

    async with session_scope() as s:
        summary = await surveys.summarize(s, survey_id)
    assert summary.total == 3
    assert summary.pending == 2
    assert summary.pending_names == ["Аня", "Боря"]
    assert summary.undelivered == 1

    async with session_scope() as s:
        pending = await surveys.pending_persons(s, survey_id)
    assert {p.full_name for p in pending} == {"Аня", "Боря"}  # Вика не в списке
