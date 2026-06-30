"""Wipe all groups, seed four demo groups for Godblazer, send live polls
to Anubis + a support-call for the imbalanced one.

Run once with the bot stopped (it sends real Telegram messages).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select

from app.bot.bot import build_bot
from app.bot.keyboards import support_ask_keyboard
from app.core.config import settings
from app.db.models import (
    Answer,
    Gender,
    Group,
    Person,
    SupportCallStatus,
    Venue,
)
from app.db.session import init_db, session_scope
from app.scheduler.scheduler import _maybe_recruit, _poll_group
from app.services import admins as admins_svc
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import schools as schools_svc
from app.services import supports as supports_svc
from app.services import surveys as surveys_svc
from app.services import venues as venues_svc

GODBLAZE_TG = 791668206
ANUBIS_TG = 6664457053


async def main() -> None:
    await init_db()
    bot = build_bot()
    tz = ZoneInfo(settings.timezone)
    now_local = datetime.now(tz).replace(microsecond=0)

    # Lesson ~2 hours from now — pick a real future minute so next_lesson_at
    # picks today, not «next week's midnight». weekdays/lesson_time живут в
    # школьном TZ, поэтому считаем их по локальному времени...
    lesson_local = (now_local + timedelta(hours=2)).replace(second=0, microsecond=0)
    weekday_today = str(lesson_local.weekday())
    lesson_time_str = lesson_local.strftime("%H:%M")
    # ...а scheduler-функции работают в aware UTC — конвертируем.
    now = now_local.astimezone(UTC)
    lesson_at = lesson_local.astimezone(UTC)

    try:
        async with session_scope() as s:
            # 1. wipe groups + venues (cascade kills memberships/surveys/calls/claims)
            await s.execute(delete(Group))
            await s.execute(delete(Venue))
            await s.flush()

            teacher = await admins_svc.get_by_tg_id(s, GODBLAZE_TG)
            assert teacher is not None, "Godblazer teacher row missing"
            school = await schools_svc.get_or_create_owned_school(s, teacher.id)
            sid = school.id

            # 2. one shared venue
            venue = await venues_svc.create_venue(
                s, sid, "Wave Dance", "Шлюзовая наб. 8с2"
            )

            # 3. four groups
            g_bachata = await groups_svc.create_group(
                s, sid, "Бачата · ур.2", weekday_today, lesson_time_str, 18,
                style="Бачата", level=2, venue_id=venue.id, room="зал 38",
                teachers_note="Маша + Петя",
            )
            g_salsa = await groups_svc.create_group(
                s, sid, "Сальса · ур.1", weekday_today, lesson_time_str, 18,
                style="Сальса", level=1, venue_id=venue.id, room="зал 40",
                teachers_note="Алина",
            )
            g_kizomba = await groups_svc.create_group(
                s, sid, "Кизомба · ур.3", weekday_today, lesson_time_str, 18,
                style="Кизомба", level=3, venue_id=venue.id, room="зал 22",
            )
            # imbalanced group — NO Anubis here, so he qualifies as a support
            g_hustle = await groups_svc.create_group(
                s, sid, "Хастл · ур.2", weekday_today, lesson_time_str, 18,
                style="Хастл", level=2, venue_id=venue.id, room="зал 19",
                teachers_note="Дима",
            )

            # 4. enrol Anubis into the first three
            anubis = await persons_svc.get_person(s, sid, ANUBIS_TG)
            assert anubis is not None
            for g in (g_bachata, g_salsa, g_kizomba):
                await persons_svc.add_to_group(
                    s, g, ANUBIS_TG, anubis.tg_username, anubis.full_name
                )

            # 5. populate g_hustle with four females (so deficit = 4, male needed)
            females = (
                await s.execute(
                    select(Person).where(
                        Person.school_id == sid, Person.gender == Gender.female
                    )
                )
            ).scalars().all()
            picked_females = females[:4]
            for p in picked_females:
                await persons_svc.add_to_group(
                    s, g_hustle, p.tg_user_id, p.tg_username, p.full_name
                )

            # add one extra non-Anubis male to the bachata group (so the poll feels real)
            extra_male = (
                await s.execute(
                    select(Person).where(
                        Person.school_id == sid,
                        Person.gender == Gender.male,
                        Person.tg_user_id != ANUBIS_TG,
                    ).limit(1)
                )
            ).scalar_one_or_none()
            if extra_male is not None:
                await persons_svc.add_to_group(
                    s, g_bachata, extra_male.tg_user_id,
                    extra_male.tg_username, extra_male.full_name,
                )

            bachata_id = g_bachata.id
            salsa_id = g_salsa.id
            hustle_id = g_hustle.id
            female_person_ids = [p.id for p in picked_females]

        # 6. live polls for the two groups Anubis sits in
        async with session_scope() as s:
            g_bachata = await groups_svc.get_group(s, bachata_id)
            g_salsa = await groups_svc.get_group(s, salsa_id)
            g_hustle = await groups_svc.get_group(s, hustle_id)
        await _poll_group(bot, g_bachata, lesson_at, now, tz=settings.timezone)
        await _poll_group(bot, g_salsa, lesson_at, now, tz=settings.timezone)

        # 7. for the hustle group: poll, mark every girl as "yes", then recruit
        await _poll_group(bot, g_hustle, lesson_at, now, tz=settings.timezone)
        async with session_scope() as s:
            survey = await surveys_svc.find_attendance_survey(
                s, hustle_id, lesson_at
            )
            assert survey is not None
            for pid in female_person_ids:
                await surveys_svc.record_answer(s, survey.id, pid, Answer.yes)

        # 8. recruit drive — picker goes to Godblazer
        recruit_now = lesson_at - timedelta(hours=2)
        await _maybe_recruit(bot, g_hustle, lesson_at, recruit_now, tz=settings.timezone)

        # 9. shortcut: open the call and message Anubis directly,
        #    so the user can answer right away instead of tapping the picker.
        async with session_scope() as s:
            survey = await surveys_svc.find_attendance_survey(
                s, hustle_id, lesson_at
            )
            call = await supports_svc.get_call_for_survey(s, survey.id)
            assert call is not None
            await supports_svc.set_status(s, call.id, SupportCallStatus.open)
            call_id = call.id

        text = (
            "🆘 Нужна помощь!\n"
            f"«Хастл · ур.2» — {lesson_at:%d.%m} в {lesson_at:%H:%M}.\n"
            "📍 Wave Dance — Шлюзовая наб. 8с2, зал 19\n"
            "👤 Дима\n"
            "Не хватает пары — сможешь подойти выручить?"
        )
        await bot.send_message(
            ANUBIS_TG, text, reply_markup=support_ask_keyboard(call_id)
        )

        print("Done. Polls sent to Anubis for: Бачата, Сальса. "
              "Support call sent for: Хастл.")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
