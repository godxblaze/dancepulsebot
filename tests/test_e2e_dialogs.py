"""E2E for the aiogram-dialog wizards, driven through a real Dispatcher with a
MockMessageManager (see _dialog_harness). Covers create-school, new-group,
edit-group, edit-name, rename-school and broadcast flows end to end."""
from __future__ import annotations

from datetime import UTC, datetime

from app.db.models import Admin, Gender, SurveyKind
from app.db.session import session_scope
from app.services import admins as admins_svc
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import schools as schools_svc
from app.services import surveys as surveys_svc
from tests._dialog_harness import click, click_plain, make_client
from tests._e2e_helpers import TEACHER_TG, make_group

TG = TEACHER_TG


async def _fresh_admin(tg_id: int = TG, name: str = "Препод") -> None:
    """Insert an admin with NO owned school (bypasses the autouse school patch)."""
    async with session_scope() as s:
        s.add(Admin(tg_user_id=tg_id, full_name=name, has_completed_setup=False))


async def test_create_school_dialog(db) -> None:
    await _fresh_admin()
    client, mock, bot = make_client(TG)

    # /start on a fresh admin → CreateSchool wizard opens
    await client.send("/start")
    assert "название своей школе" in mock.last_message().text

    # type the school name → school is created
    await client.send("Wave Dance")
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, TG)
        school = await schools_svc.get_owned_school(s, admin.id)
        assert school is not None and school.name == "Wave Dance"
    assert any("Wave Dance" in t and "создана" in t for t in bot.texts())


async def test_new_group_dialog(db) -> None:
    # admin already has a school (make_group creates it) → drive /newgroup wizard
    await make_group()
    client, mock, bot = make_client(TG)

    await client.send("/newgroup")
    # style window → type a custom style
    await click(client, mock, "Ввести своё")
    await client.send("Кизомба")
    # level window → pick ур.2
    await click(client, mock, "ур.2")
    # weekdays → tick Вт, then Готово
    await click(client, mock, "Вт")
    await click(client, mock, "Готово")
    # time
    await client.send("20:30")
    # venue → add a new one
    await click(client, mock, "Добавить новую")
    await client.send("Студия Полевик")
    await client.send("Шлюзовая 8")
    # room → type
    await client.send("зал 7")
    # teachers → type → finishes
    await client.send("Маша + Петя")

    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, TG)
        groups = await groups_svc.admin_visible_groups(s, admin.id)
        # the wizard-made group is the one with style Кизомба
        kizomba = [g for g in groups if g.style == "Кизомба"]
        assert kizomba, [g.style for g in groups]
        g = kizomba[0]
        assert g.level == 2
        assert g.weekdays == "1"  # Вт == index 1
        assert g.lesson_time == "20:30"
        assert g.room == "зал 7"
        assert g.teachers_note == "Маша + Петя"
        venue = await __get_venue(s, g.venue_id)
        assert venue is not None and venue.name == "Студия Полевик"
    # invite message with the QR/link went out
    assert any("Приглашение" in t for t in bot.texts())


async def __get_venue(s, venue_id):
    from app.services import venues as venues_svc
    return await venues_svc.get_venue(s, venue_id)


async def test_new_group_dialog_skip_path(db) -> None:
    """The all-skip path: no style/level/venue/room/teachers — just days + time."""
    await make_group()
    client, mock, bot = make_client(TG)

    await client.send("/newgroup")
    await click(client, mock, "Пропустить")        # style skip
    await click(client, mock, "Пропустить")        # level skip
    await click(client, mock, "Пн")                # weekday
    await click(client, mock, "Готово")
    await client.send("18:00")                      # time
    await click(client, mock, "Пропустить")        # venue skip
    await click(client, mock, "Пропустить")        # room skip
    await click(client, mock, "Пропустить")        # teachers skip → finish

    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, TG)
        groups = await groups_svc.admin_visible_groups(s, admin.id)
        # a minimal group with weekday Пн (index 0), time 18:00, no style
        minimal = [g for g in groups if g.lesson_time == "18:00" and g.style is None]
        assert minimal, [(g.style, g.lesson_time) for g in groups]


async def test_edit_name_dialog(db) -> None:
    gid, sid, code = await make_group()
    client, mock, bot = make_client(TG)

    await client.send("/name")
    assert "Как тебя называть" in mock.last_message().text
    await client.send("Мария Ивановна")
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, TG)
        assert admin.full_name == "Мария Ивановна"
    assert any("Мария Ивановна" in t for t in bot.texts())


async def test_rename_school_dialog(db) -> None:
    gid, sid, code = await make_group()
    client, mock, bot = make_client(TG)

    # open the school card, then tap "Переименовать школу" → rename wizard
    await client.send("/school")
    await click_plain(client, bot, "Переименовать школу")
    assert "новое название" in mock.last_message().text
    await client.send("Школа Танца")
    async with session_scope() as s:
        assert (await schools_svc.get_school(s, sid)).name == "Школа Танца"


async def test_broadcast_dialog(db) -> None:
    gid, sid, code = await make_group()
    client, mock, bot = make_client(TG)
    # two students to receive the broadcast
    async with session_scope() as s:
        group = await groups_svc.get_group(s, gid)
        for tg, name in ((4001, "Аня"), (4002, "Боря")):
            p, _ = await persons_svc.add_to_group(s, group, tg, "u", name)
            await persons_svc.set_gender(s, p.id, Gender.female)

    # make the teacher responsible so /groups lists the group
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, TG)
        await groups_svc.set_group_admin(s, gid, admin.id, True)
    # open group card → "Сообщение" → broadcast wizard
    await client.send("/groups")
    await click_plain(client, bot, "Бачата")    # group button → card
    await click_plain(client, bot, "Сообщение")  # → broadcast wizard
    assert "отправлю всем" in mock.last_message().text
    await client.send("Завтра отмена, всем привет")
    # confirm preview → send
    await click(client, mock, "Отправить")
    # both students got the broadcast text
    bodies = bot.texts()
    assert sum(1 for t in bodies if "Завтра отмена" in t) >= 2


async def test_edit_group_dialog(db) -> None:
    gid, sid, code = await make_group()
    client, mock, bot = make_client(TG)
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, TG)
        await groups_svc.set_group_admin(s, gid, admin.id, True)

    await client.send("/groups")
    await click_plain(client, bot, "Бачата")     # group card
    await click_plain(client, bot, "Изменить")   # → edit-group wizard menu

    async def get_group():
        async with session_scope() as s:
            return await groups_svc.get_group(s, gid)

    # time
    await click(client, mock, "Время")
    await client.send("21:15")
    assert (await get_group()).lesson_time == "21:15"

    # level: pick then clear
    await click(client, mock, "Уровень")
    await click(client, mock, "ур.3")
    assert (await get_group()).level == 3
    await click(client, mock, "Уровень")
    await click(client, mock, "Очистить")
    assert (await get_group()).level is None

    # style: type custom, then pick from suggestions, then clear
    await click(client, mock, "Направление")
    await click(client, mock, "Ввести своё")
    await client.send("Хастл")
    assert (await get_group()).style == "Хастл"
    await click(client, mock, "Направление")
    await click(client, mock, "Хастл")            # suggestion Select → _on_style_pick
    assert (await get_group()).style == "Хастл"
    await click(client, mock, "Направление")
    await click(client, mock, "Очистить")
    assert (await get_group()).style is None

    # weekdays: add Ср, keep selection, done
    await click(client, mock, "Дни недели")
    await click(client, mock, "Ср")
    await click(client, mock, "Готово")
    assert "2" in (await get_group()).weekdays

    # room: type then clear
    await click(client, mock, "Зал")
    await client.send("зал 9")
    assert (await get_group()).room == "зал 9"
    await click(client, mock, "Зал")
    await click(client, mock, "Очистить")
    assert (await get_group()).room is None

    # teachers: type then clear
    await click(client, mock, "Преподаватели")
    await client.send("Катя")
    assert (await get_group()).teachers_note == "Катя"
    await click(client, mock, "Преподаватели")
    await click(client, mock, "Очистить")
    assert (await get_group()).teachers_note is None

    # venue: add new (with address), then pick it, then clear
    await click(client, mock, "Место")
    await click(client, mock, "Добавить новую")
    await client.send("Лофт")
    await client.send("Тверская 1")
    async with session_scope() as s:
        g = await groups_svc.get_group(s, gid)
        assert g.venue_id is not None
        from app.services import venues as venues_svc
        assert (await venues_svc.get_venue(s, g.venue_id)).name == "Лофт"
    await click(client, mock, "Место")
    await click(client, mock, "Лофт")             # Select existing → _on_venue_pick
    await click(client, mock, "Место")
    await click(client, mock, "Очистить")
    assert (await get_group()).venue_id is None

    # finish
    await click(client, mock, "К группе")
    assert any("Готово" in t for t in bot.texts())


async def test_edit_group_dialog_notifies_on_reschedule_with_open_survey(db) -> None:
    """Шаг 16: правка lesson_time с открытым опросом закрывает его и уведомляет
    ответственного через реальный dialog-flow (не только сервисный юнит-тест).
    Заголовок группы с HTML-спецсимволом («Rock & Roll») должен уйти escape()'нутым
    (raw wire-текст содержит «&amp;») ОБЯЗАТЕЛЬНО в паре с parse_mode=HTML —
    иначе Telegram показал бы «&amp;» буквально вместо «&»."""
    from aiogram.enums import ParseMode
    from aiogram.methods import SendMessage

    gid, sid, code = await make_group()
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, TG)
        await groups_svc.set_group_admin(s, gid, admin.id, True)
        group = await groups_svc.get_group(s, gid)
        group.style = "Rock & Roll"
        survey, _ = await surveys_svc.get_or_create_survey(
            s, gid, SurveyKind.attendance, datetime(2026, 6, 1, 19, 0, tzinfo=UTC)
        )
        survey_id = survey.id

    client, mock, bot = make_client(TG)
    await client.send("/groups")
    await click_plain(client, bot, "Rock & Roll")
    await click_plain(client, bot, "Изменить")
    await click(client, mock, "Время")
    await client.send("20:00")

    async with session_scope() as s:
        survey = await surveys_svc.get_survey(s, survey_id)
        assert survey.status.value == "closed"

    notify_calls = [
        m for m in bot.records
        if isinstance(m, SendMessage) and "Расписание группы" in (m.text or "")
    ]
    assert len(notify_calls) == 1
    assert "Rock &amp; Roll" in notify_calls[0].text  # escape() применён
    assert notify_calls[0].parse_mode == ParseMode.HTML  # ...и в паре с HTML-режимом


async def test_edit_group_venue_skip_address(db) -> None:
    """The add-venue-without-address branch (_on_venue_new_addr_skip)."""
    gid, sid, code = await make_group()
    client, mock, bot = make_client(TG)
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, TG)
        await groups_svc.set_group_admin(s, gid, admin.id, True)

    await client.send("/groups")
    await click_plain(client, bot, "Бачата")
    await click_plain(client, bot, "Изменить")
    await click(client, mock, "Место")
    await click(client, mock, "Добавить новую")
    await client.send("Зал без адреса")
    await click(client, mock, "Пропустить")   # skip address
    async with session_scope() as s:
        g = await groups_svc.get_group(s, gid)
        from app.services import venues as venues_svc
        v = await venues_svc.get_venue(s, g.venue_id)
        assert v.name == "Зал без адреса" and v.address is None
