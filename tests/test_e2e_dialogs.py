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

    # type the school name → school is created, wizard asks for the place
    await client.send("Wave Dance")
    assert "Где проходят занятия" in mock.last_message().text
    await client.send("Тверская 10")
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, TG)
        school = await schools_svc.get_owned_school(s, admin.id)
        assert school is not None and school.name == "Wave Dance"
        from app.services import venues as venues_svc
        venues = await venues_svc.school_venues(s, school.id)
        assert [v.name for v in venues] == ["Тверская 10"]
    assert any("Wave Dance" in t and "создана" in t for t in bot.texts())


async def test_create_school_dialog_place_skip(db) -> None:
    """«Пропустить» на шаге места — школа без площадок, финал тот же."""
    await _fresh_admin()
    client, mock, bot = make_client(TG)

    await client.send("/start")
    await client.send("Wave Dance")
    await click(client, mock, "Пропустить")
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, TG)
        school = await schools_svc.get_owned_school(s, admin.id)
        assert school is not None
        from app.services import venues as venues_svc
        assert await venues_svc.school_venues(s, school.id) == []
    assert any("создана" in t for t in bot.texts())


async def test_create_school_dialog_honest_when_school_already_exists(db) -> None:
    """Если у admin'а УЖЕ есть owned-школа (напр. гонка: другой процесс/тап
    успел создать её, пока wizard был открыт), сообщение не должно лгать про
    «создана» с только что введённым именем — честно говорим, что школа уже
    есть, и называем её реальное (старое) имя."""
    from types import SimpleNamespace

    from app.bot.dialogs.misc_dialogs import _on_create_school_typed
    from tests._fakes import FakeMessage, fake_user

    async with session_scope() as s:
        admin = await admins_svc.get_or_create(s, TG, "t", "Препод")
        # conftest даёт owned-школу автоматически ("Моя школа" по умолчанию)
        existing = await schools_svc.get_owned_school(s, admin.id)
        assert existing is not None

    class _FakeManager:
        def __init__(self, user) -> None:
            self.event = SimpleNamespace(from_user=user)
            self.done_calls = 0

        async def done(self) -> None:
            self.done_calls += 1

    user = fake_user(TG, full_name="Препод")
    msg = FakeMessage(user, text="Другое Имя")
    manager = _FakeManager(user)
    await _on_create_school_typed(msg, None, manager, "Другое Имя")

    assert msg.replies
    text = msg.replies[0].text
    assert "уже есть школа" in text and "не стал" in text
    assert existing.name in text
    async with session_scope() as s:
        school = await schools_svc.get_owned_school(s, admin.id)
        assert school.name != "Другое Имя"  # не переименовалась молча


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
    # time — свой текст всё ещё принимается наравне с кнопками 12:00–22:00
    await client.send("20:30")
    # duration → pick 1,5 ч
    await click(client, mock, "1,5 ч")
    # venue → одной строкой, без отдельного вопроса про адрес
    await client.send("Студия Полевик")
    # room → type
    await client.send("зал 7")
    # teachers → type → finishes (и сохраняется как связка школы)
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
        assert g.duration_min == 90
        assert g.room == "зал 7"
        assert g.teachers_note == "Маша + Петя"
        venue = await __get_venue(s, g.venue_id)
        assert venue is not None and venue.name == "Студия Полевик"
        from app.services import teacher_presets as presets_svc
        presets = await presets_svc.school_presets(s, g.school_id)
        assert [p.note for p in presets] == ["Маша + Петя"]
    # invite message with the QR/link went out
    assert any("Приглашение" in t for t in bot.texts())


async def test_new_group_dialog_time_buttons_and_teacher_preset(db) -> None:
    """Кнопки времени 12:00–22:00, кнопки длительности и тап по сохранённой
    связке преподавателей — путь «всё кнопками» без единого текстового ввода
    (кроме /newgroup)."""
    from app.services import teacher_presets as presets_svc

    gid, sid, code = await make_group()
    async with session_scope() as s:
        await presets_svc.get_or_create(s, sid, "Маша + Петя")

    client, mock, bot = make_client(TG)
    await client.send("/newgroup")
    await click(client, mock, "Сальса")           # дефолтная подсказка стиля
    await click(client, mock, "ур.1")
    await click(client, mock, "Пн")
    await click(client, mock, "Готово")
    await click(client, mock, "19:00")             # кнопка времени
    assert "длится" in mock.last_message().text
    await click(client, mock, "2 ч")               # кнопка длительности
    await click(client, mock, "Пропустить")        # venue skip
    await click(client, mock, "Пропустить")        # room skip
    await click(client, mock, "Маша + Петя")       # преподаватели — пресет

    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, TG)
        groups = await groups_svc.admin_visible_groups(s, admin.id)
        salsa = [g for g in groups if g.style == "Сальса"]
        assert salsa, [g.style for g in groups]
        g = salsa[0]
        assert g.lesson_time == "19:00"
        assert g.duration_min == 120
        assert g.teachers_note == "Маша + Петя"
    assert any("Приглашение" in t for t in bot.texts())


async def __get_venue(s, venue_id):
    from app.services import venues as venues_svc
    return await venues_svc.get_venue(s, venue_id)


async def test_new_group_dialog_direct_text_on_style_window(db) -> None:
    """Раньше style-окно принимало текст, только если сначала тапнуть «Ввести
    своё» — прямой текст без тапа не продвигал мастер, а бесконечно повторял
    тот же вопрос (реальный баг из пилота 2026-07-07). Теперь TextInput стоит
    прямо на style-окне."""
    await make_group()
    client, mock, bot = make_client(TG)

    await client.send("/newgroup")
    assert "Какое направление" in mock.last_message().text
    await client.send("Бачата")  # без тапа "Ввести своё"
    assert "Какой уровень" in mock.last_message().text  # продвинулся, не повторил


async def test_new_group_dialog_direct_text_on_venue_window(db) -> None:
    """Текст на venue-окне заводит площадку одной строкой (без отдельного
    вопроса про адрес) и сразу двигает мастер на шаг зала."""
    await make_group()
    client, mock, bot = make_client(TG)

    await client.send("/newgroup")
    await click(client, mock, "Пропустить")   # style
    await click(client, mock, "Пропустить")   # level
    await click(client, mock, "Пн")
    await click(client, mock, "Готово")
    await client.send("18:00")
    await click(client, mock, "Пропустить")   # duration
    assert "Где проходят занятия" in mock.last_message().text
    await client.send("Тверская 10")      # адрес как есть — это и есть площадка
    assert "зале" in mock.last_message().text.lower()
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, TG)
        school = await schools_svc.get_owned_school(s, admin.id)
        from app.services import venues as venues_svc
        names = [v.name for v in await venues_svc.school_venues(s, school.id)]
        assert "Тверская 10" in names


async def test_new_group_dialog_cancel_from_style_window(db) -> None:
    """Единственная точка выхода из мастера «Новая группа» без прохождения
    всех 9 шагов — Cancel на первом окне (все остальные окна доступны через
    «◀ Назад», который в итоге ведёт сюда). Отмена не должна быть немой —
    иначе на пилоте выглядит, будто бот завис."""
    await make_group()
    client, mock, bot = make_client(TG)

    await client.send("/newgroup")
    await click(client, mock, "Отмена")
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, TG)
        groups = await groups_svc.admin_visible_groups(s, admin.id)
    assert len(groups) == 1  # только группа из make_group, мастер не создал вторую
    assert any("Отменено" in t for t in bot.texts())


async def test_new_group_dialog_survives_start_then_resets(db) -> None:
    """Брошенный на середине мастер не должен переживать /start и перехватывать
    дальнейшие тапы по нижнему меню (см. инцидент 2026-07-07: заброшенный
    /newgroup поглощал тап «Школа» и повторял «Какое направление?»)."""
    await make_group()
    client, mock, bot = make_client(TG)

    await client.send("/newgroup")
    assert "Какое направление" in mock.last_message().text  # мастер открыт, брошен

    await client.send("/start")  # safe harbor — должен сбросить зависший стек
    await client.send("🏫 Школа")
    # school card came through as a plain message, not another dialog prompt
    assert any("Состав" in t for t in bot.texts())


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
    await click(client, mock, "Пропустить")        # duration skip
    await click(client, mock, "Пропустить")        # venue skip
    await click(client, mock, "Пропустить")        # room skip
    await click(client, mock, "Пропустить")        # teachers skip → finish

    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, TG)
        groups = await groups_svc.admin_visible_groups(s, admin.id)
        # a minimal group with weekday Пн (index 0), time 18:00, no style
        minimal = [g for g in groups if g.lesson_time == "18:00" and g.style is None]
        assert minimal, [(g.style, g.lesson_time) for g in groups]


async def test_finish_new_group_idempotent_on_double_invocation(db) -> None:
    """Двойной тап «Пропустить» на шаге «преподаватели» (или Enter сразу после
    тапа) раньше создавал ДВЕ одинаковые группы — _finish_new_group теперь
    ставит синхронный флаг _finishing до первого await, второй вызов должен
    быть no-op."""
    from types import SimpleNamespace

    from app.bot.dialogs.new_group import _finish_new_group
    from tests._fakes import FakeBot, FakeMessage, fake_user

    await make_group(teacher_tg=850, teacher_name="Одна группа")

    class _FakeManager:
        def __init__(self, dialog_data, bot, user) -> None:
            self.dialog_data = dialog_data
            self.start_data = None
            self.middleware_data = {"bot": bot}
            self.event = SimpleNamespace(from_user=user)
            self.done_calls = 0

        async def done(self) -> None:
            self.done_calls += 1

    bot = FakeBot()
    manager = _FakeManager(
        {"weekdays": [0], "lesson_time": "21:00", "style": "Идемпотентная"},
        bot, fake_user(850, full_name="Одна группа"),
    )
    target = FakeMessage(fake_user(850, full_name="Одна группа"), bot=bot)

    await _finish_new_group(target, manager)
    await _finish_new_group(target, manager)  # тот же dialog_data — уже _finishing

    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, 850)
        groups = await groups_svc.admin_visible_groups(s, admin.id)
    matching = [g for g in groups if g.style == "Идемпотентная"]
    assert len(matching) == 1  # не задвоилась


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


async def test_broadcast_dialog_cancel_shows_group_card(db) -> None:
    """«Отмена» в broadcast раньше молча снимала клавиатуру. Диалог открылся
    из карточки группы и съел её — возвращаем карточку заново."""
    gid, sid, code = await make_group()
    client, mock, bot = make_client(TG)
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, TG)
        await groups_svc.set_group_admin(s, gid, admin.id, True)

    await client.send("/groups")
    await click_plain(client, bot, "Бачата")
    await click_plain(client, bot, "Сообщение")
    assert "отправлю всем" in mock.last_message().text

    before = len(bot.outbox)
    await click(client, mock, "Отмена")
    cards = [m for m in bot.outbox[before:] if "Бачата" in (m.text or "")]
    assert cards, "после «Отмена» не вернулась карточка группы"


async def test_rename_school_dialog_cancel_feedback(db) -> None:
    """Cancel в остальных диалогах шлёт «⏭ Отменено», а не молчит."""
    gid, sid, code = await make_group()
    client, mock, bot = make_client(TG)

    await client.send("/school")
    await click_plain(client, bot, "Переименовать школу")
    assert "новое название" in mock.last_message().text
    await click(client, mock, "Отмена")
    assert any("Отменено" in t for t in bot.texts())
    async with session_scope() as s:
        assert (await schools_svc.get_school(s, sid)).name != "Отменено"


async def test_edit_group_dialog_direct_text_on_style_window(db) -> None:
    """Тот же паттерн, что в new_group: style-окно должно принимать текст
    напрямую, без обязательного тапа «Ввести своё»."""
    gid, sid, code = await make_group()
    client, mock, bot = make_client(TG)
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, TG)
        await groups_svc.set_group_admin(s, gid, admin.id, True)

    await client.send("/groups")
    await click_plain(client, bot, "Бачата")
    await click_plain(client, bot, "Изменить")
    await click(client, mock, "Направление")
    await client.send("Кизомба")  # без тапа "Ввести своё"
    async with session_scope() as s:
        assert (await groups_svc.get_group(s, gid)).style == "Кизомба"


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

    # duration: pick then clear
    await click(client, mock, "Длительность")
    await click(client, mock, "1,5 ч")
    assert (await get_group()).duration_min == 90
    await click(client, mock, "Длительность")
    await click(client, mock, "Очистить")
    assert (await get_group()).duration_min is None

    # venue: add new одной строкой, then pick it, then clear
    await click(client, mock, "Место")
    await client.send("Лофт")
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

    # finish → настоящая карточка группы (см. отдельный тест ниже)
    await click(client, mock, "К группе")
    assert bot.find_button_message("Ученики") is not None


async def test_edit_group_dialog_back_to_group_shows_card(db) -> None:
    """«◀ К группе» из меню редактора должен показать карточку группы —
    как одноимённые кнопки в «Ответственных» и «Зале на занятие», а не
    отсылать к /groups текстом (живой e2e-прогон 2026-07-08)."""
    gid, sid, code = await make_group()
    client, mock, bot = make_client(TG)
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, TG)
        await groups_svc.set_group_admin(s, gid, admin.id, True)

    await client.send("/groups")
    await click_plain(client, bot, "Бачата")
    await click_plain(client, bot, "Изменить")
    assert "Что меняем" in mock.last_message().text

    before = len(bot.outbox)
    await click(client, mock, "К группе")
    cards = [m for m in bot.outbox[before:] if "Бачата" in (m.text or "")]
    assert cards, "после «К группе» не пришла карточка группы"
    buttons = [
        b.text for row in cards[-1].reply_markup.inline_keyboard for b in row
    ]
    assert any("Ученики" in t for t in buttons), buttons
    assert any("Изменить" in t for t in buttons), buttons


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


async def test_edit_group_venue_one_line(db) -> None:
    """Новая площадка в редакторе — одной строкой, без вопроса про адрес."""
    gid, sid, code = await make_group()
    client, mock, bot = make_client(TG)
    async with session_scope() as s:
        admin = await admins_svc.get_by_tg_id(s, TG)
        await groups_svc.set_group_admin(s, gid, admin.id, True)

    await client.send("/groups")
    await click_plain(client, bot, "Бачата")
    await click_plain(client, bot, "Изменить")
    await click(client, mock, "Место")
    await client.send("Зал без адреса")
    async with session_scope() as s:
        g = await groups_svc.get_group(s, gid)
        from app.services import venues as venues_svc
        v = await venues_svc.get_venue(s, g.venue_id)
        assert v.name == "Зал без адреса" and v.address is None
    # мастер вернулся в меню редактирования
    assert "Что меняем" in mock.last_message().text


async def test_school_venues_management(db) -> None:
    """Меню школы: 📍 Площадки — список, добавление через диалог, удаление
    с подтверждением."""
    gid, sid, code = await make_group()
    client, mock, bot = make_client(TG)

    await client.send("🏫 Школа")
    await click_plain(client, bot, "📍 Площадки")
    assert any("Пока пусто" in t for t in bot.texts())

    # add via dialog
    await click_plain(client, bot, "➕ Добавить площадку")
    await client.send("Тверская 10")
    async with session_scope() as s:
        from app.services import venues as venues_svc
        names = [v.name for v in await venues_svc.school_venues(s, sid)]
        assert names == ["Тверская 10"]

    # re-open the list → venue row is there; delete with confirm
    await click_plain(client, bot, "К площадкам")
    await click_plain(client, bot, "🗑 Тверская 10")
    assert any("Удалить площадку" in t for t in bot.texts())
    await click_plain(client, bot, "Да, удалить")
    async with session_scope() as s:
        from app.services import venues as venues_svc
        assert await venues_svc.school_venues(s, sid) == []


async def test_school_teacher_presets_management(db) -> None:
    """Меню школы: 👤 Преподаватели — добавление связки и удаление тапом."""
    gid, sid, code = await make_group()
    client, mock, bot = make_client(TG)

    await client.send("🏫 Школа")
    await click_plain(client, bot, "👤 Преподаватели")
    await click_plain(client, bot, "➕ Добавить связку")
    await client.send("Маша + Петя")
    async with session_scope() as s:
        from app.services import teacher_presets as presets_svc
        assert [p.note for p in await presets_svc.school_presets(s, sid)] == [
            "Маша + Петя"
        ]

    await click_plain(client, bot, "К преподавателям")
    await click_plain(client, bot, "🗑 Маша + Петя")
    async with session_scope() as s:
        from app.services import teacher_presets as presets_svc
        assert await presets_svc.school_presets(s, sid) == []


async def test_teacher_presets_get_or_create_dedup(db) -> None:
    """Повторный ввод той же связки в мастере не плодит дубликатов."""
    from app.services import teacher_presets as presets_svc

    gid, sid, code = await make_group()
    async with session_scope() as s:
        p1 = await presets_svc.get_or_create(s, sid, "Маша + Петя")
        p2 = await presets_svc.get_or_create(s, sid, "Маша + Петя")
        assert p1.id == p2.id
        assert len(await presets_svc.school_presets(s, sid)) == 1
