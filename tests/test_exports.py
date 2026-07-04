"""attendance_csv — lesson_at должен рендериться в TZ школы, а не в глобальном
settings.timezone (Europe/Moscow) по умолчанию."""
import csv
import io
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.db.models import Answer, SurveyKind
from app.db.session import session_scope
from app.services import admins, groups, persons, schools, surveys
from app.services.exports import attendance_csv

_NSK = ZoneInfo("Asia/Novosibirsk")  # UTC+7 — заведомо ≠ Europe/Moscow (UTC+3)
LESSON = datetime(2026, 5, 25, 19, 0, tzinfo=_NSK).astimezone(UTC)


async def test_attendance_csv_uses_school_timezone(db) -> None:
    async with session_scope() as s:
        teacher = await admins.get_or_create(s, 111, "t", "Препод")
        group = await groups.create_group(s, teacher.id, "Группа A", "0", "19:00", 18)
        school_id = group.school_id
        await schools.set_timezone(s, school_id, "Asia/Novosibirsk")
        p, _ = await persons.add_to_group(s, group, 200, "u", "Ученик")
        survey, _ = await surveys.get_or_create_survey(
            s, group.id, SurveyKind.attendance, LESSON
        )
        await surveys.record_answer(s, survey.id, p.id, Answer.yes)

    async with session_scope() as s:
        data = await attendance_csv(s, school_id)

    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    header, row = rows[0], rows[1]
    lesson_at_col = header.index("lesson_at")
    # LESSON — ровно 19:00 в Asia/Novosibirsk; неправильный TZ (Europe/Moscow,
    # -4ч) дал бы "2026-05-25 15:00".
    assert row[lesson_at_col] == "2026-05-25 19:00"
