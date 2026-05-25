"""Wipe all groups, seed four demo groups for Godblazer, send live polls
to Anubis + a support-call for the imbalanced one.

Run once with the bot stopped (it sends real Telegram messages).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
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
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import supports as supports_svc
from app.services import surveys as surveys_svc
from app.services import teachers as teachers_svc
from app.services import venues as venues_svc

GODBLAZE_TG = 791668206
ANUBIS_TG = 6664457053


async def main() -> None:
    await init_db()
    bot = build_bot()
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz).replace(microsecond=0)

    # Lesson ~2 hours from now — pick a real future minute so next_lesson_at
    # picks today, not «next week's midnight».
    lesson_at = (now + timedelta(hours=2)).replace(second=0, microsecond=0)
    lesson_at = lesson_at.replace(tzinfo=None)  # scheduler uses naive datetimes
    weekday_today = str(lesson_at.weekday())
    lesson_time_str = lesson_at.strftime("%H:%M")

    try:
        async with session_scope() as s:
            # 1. wipe groups + venues (cascade kills memberships/surveys/calls/claims)
            await s.execute(delete(Group))
            await s.execute(delete(Venue))
            await s.flush()

            teacher = await teachers_svc.get_by_tg_id(s, GODBLAZE_TG)
            assert teacher is not None, "Godblazer teacher row missing"
            tid = teacher.id

            # 2. one shared venue
            venue = await venues_svc.create_venue(
                s, tid, "Wave Dance", "Шлюзовая наб. 8с2"
            )

            # 3. four groups
            g_bachata = await groups_svc.create_group(
                s, tid, "Бачата · ур.2", weekday_today, lesson_time_str, 18,
                style="Бачата", level=2, venue_id=venue.id, room="зал 38",
                teachers_note="Маша + Петя",
            )
            g_salsa = await groups_svc.create_group(
                s, tid, "Сальса · ур.1", weekday_today, lesson_time_str, 18,
                style="Сальса", level=1, venue_id=venue.id, room="зал 40",
                teachers_note="Алина",
            )
            g_kizomba = await groups_svc.create_group(
                s, tid, "Кизомба · ур.3", weekday_today, lesson_time_str, 18,
                style="Кизомба", level=3, venue_id=venue.id, room="зал 22",
            )
            # imbalanced group — NO Anubis here, so he qualifies as a support
            g_hustle = await groups_svc.create_group(
                s, tid, "Хастл · ур.2", weekday_today, lesson_time_str, 18,
                style="Хастл", level=2, venue_id=venue.id, room="зал 19",
                teachers_note="Дима",
            )

            # 4. enrol Anubis into the first three
            anubis = await persons_svc.get_person(s, tid, ANUBIS_TG)
            assert anubis is not None
            for g in (g_bachata, g_salsa, g_kizomba):
                await persons_svc.add_to_group(
                    s, g, ANUBIS_TG, anubis.tg_username, anubis.full_name
                )

            # 5. populate g_hustle with four females (so deficit = 4, male needed)
            females = (
                await s.execute(
                    select(Person).where(
                        Person.teacher_id == tid, Person.gender == Gender.female
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
                        Person.teacher_id == tid,
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
        await _poll_group(bot, g_bachata, lesson_at)
        await _poll_group(bot, g_salsa, lesson_at)

        # 7. for the hustle group: poll, mark every girl as "yes", then recruit
        await _poll_group(bot, g_hustle, lesson_at)
        async with session_scope() as s:
            survey = await surveys_svc.find_attendance_survey(
                s, hustle_id, lesson_at
            )
            assert survey is not None
            for pid in female_person_ids:
                await surveys_svc.record_answer(s, survey.id, pid, Answer.yes)

        # 8. recruit drive — picker goes to Godblazer
        recruit_now = lesson_at - timedelta(hours=2)
        await _maybe_recruit(bot, g_hustle, lesson_at, recruit_now)

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
