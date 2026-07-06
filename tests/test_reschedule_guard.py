"""Гард правки расписания (шаг 16 аудита): смена lesson_time/weekdays группы
или TZ школы при открытом опросе не должна его осиротить — identity занятия
(пересчитываемый UTC-инстант) меняется, старый опрос закрывается и
ответственные уведомляются; будущие LessonCancellation по старому расписанию
подчищаются, чтобы не «воскреснуть» под новым."""
from datetime import UTC, datetime, timedelta

from app.bot.handlers.school import cb_school_tz_set
from app.db.models import Answer, Gender, SupportCallStatus, SurveyKind, SurveyStatus
from app.db.session import session_scope
from app.services import admins, cancellations, groups, persons, schools, supports, surveys
from tests._e2e_helpers import TEACHER_TG, cb
from tests._fakes import FakeBot, SentMessage

# close_orphaned_survey чистит cancellation'ы будущее которых сверяется с
# РЕАЛЬНЫМ utc_now() (не монкипатчится, в отличие от scheduler-тестов) —
# нужна дата, действительно лежащая в будущем от текущего момента запуска.
LESSON = datetime.now(UTC) + timedelta(days=14)


async def _group_with_open_survey() -> tuple[int, int, int]:
    async with session_scope() as s:
        t = await admins.get_or_create(s, TEACHER_TG, "t", "Препод")
        g = await groups.create_group(s, t.id, "Бачата", "0", "19:00", 18)
        survey, _ = await surveys.get_or_create_survey(
            s, g.id, SurveyKind.attendance, LESSON
        )
        p, _ = await persons.add_to_group(s, g, 700, "u", "Ученик")
        await surveys.record_answer(s, survey.id, p.id, Answer.yes)
        await groups.set_group_admin(s, g.id, t.id, True)
        return g.id, g.school_id, survey.id


async def test_lesson_time_change_closes_open_survey_and_notifies(db) -> None:
    gid, sid, survey_id = await _group_with_open_survey()
    async with session_scope() as s:
        # будущая отмена по старому расписанию — должна быть подчищена
        await cancellations.cancel(s, gid, LESSON + timedelta(days=7))
        group = await groups.get_group(s, gid)
        notify_tgs = await groups.update_group(s, group, lesson_time="20:00")

    assert TEACHER_TG in notify_tgs
    async with session_scope() as s:
        survey = await surveys.get_survey(s, survey_id)
        assert survey.status == SurveyStatus.closed
        assert not await cancellations.is_cancelled(s, gid, LESSON + timedelta(days=7))


async def test_lesson_time_change_without_open_survey_no_notify(db) -> None:
    """Регресс: правка без открытого опроса не шлёт лишних уведомлений и не
    падает на пустом наборе surveys."""
    async with session_scope() as s:
        t = await admins.get_or_create(s, TEACHER_TG, "t", "Препод")
        g = await groups.create_group(s, t.id, "Бачата", "0", "19:00", 18)
        group = await groups.get_group(s, g.id)
        notify_tgs = await groups.update_group(s, group, lesson_time="20:00")
    assert notify_tgs == []


async def test_lesson_time_change_closes_live_support_call(db) -> None:
    """close_orphaned_survey не должен оставлять добор жить сам по себе,
    отвязанным от survey: scheduler._maybe_recruit больше не найдёт его (survey
    закрыт), но лид ещё мог бы тапнуть по старому picker'у и позвать саппорта на
    занятие, которого больше нет по этому расписанию. Call создан и закоммичен
    ДО правки расписания отдельной транзакцией — как в реальности (scheduler
    заводит call вчера вечером, правка расписания происходит отдельным событием
    сегодня), а не в одной транзакции с самой правкой."""
    gid, _sid, survey_id = await _group_with_open_survey()
    async with session_scope() as s:
        call = await supports.create_call(
            s, survey_id, Gender.male, 2, SupportCallStatus.pending
        )
        call_id = call.id
    async with session_scope() as s:
        group = await groups.get_group(s, gid)
        await groups.update_group(s, group, lesson_time="20:00")
    async with session_scope() as s:
        call = await supports.get_call(s, call_id)
        assert call.status == SupportCallStatus.closed


async def test_unrelated_field_change_does_not_trigger_guard(db) -> None:
    """Изменение НЕ-расписания (напр. style) не закрывает открытый опрос."""
    gid, _sid, survey_id = await _group_with_open_survey()
    async with session_scope() as s:
        group = await groups.get_group(s, gid)
        notify_tgs = await groups.update_group(s, group, style="Хастл")
    assert notify_tgs == []
    async with session_scope() as s:
        survey = await surveys.get_survey(s, survey_id)
        assert survey.status == SurveyStatus.open  # не тронут


async def test_school_tz_change_closes_open_surveys_and_notifies(db) -> None:
    _gid, sid, survey_id = await _group_with_open_survey()
    bot = FakeBot()
    tz_msg = SentMessage(bot, TEACHER_TG, "")
    await cb_school_tz_set(
        cb(f"schtzset:{sid}:Asia/Almaty", TEACHER_TG, message=tz_msg), bot
    )
    async with session_scope() as s:
        survey = await surveys.get_survey(s, survey_id)
        assert survey.status == SurveyStatus.closed
        assert await schools.get_timezone(s, sid) == "Asia/Almaty"
    assert bot.sent_to(TEACHER_TG)  # ответственный уведомлён


async def test_school_tz_change_without_open_survey_no_notify(db) -> None:
    async with session_scope() as s:
        t = await admins.get_or_create(s, TEACHER_TG, "t", "Препод")
        g = await groups.create_group(s, t.id, "Бачата", "0", "19:00", 18)
        sid = g.school_id
    bot = FakeBot()
    tz_msg = SentMessage(bot, TEACHER_TG, "")
    await cb_school_tz_set(
        cb(f"schtzset:{sid}:Asia/Almaty", TEACHER_TG, message=tz_msg), bot
    )
    assert bot.sent_to(TEACHER_TG) == []
