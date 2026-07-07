from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from app.bot import ui
from app.bot.keyboards import (
    support_flip_keyboard,
    support_pick_keyboard,
)
from app.bot.utils import format_group_title
from app.db.models import Gender, Person, SupportCallStatus
from app.db.session import session_scope
from app.scheduler.scheduler import lesson_place, send_support_invites
from app.services import groups as groups_svc
from app.services import persons as persons_svc
from app.services import schools as schools_svc
from app.services import supports as supports_svc
from app.services import surveys as surveys_svc
from app.services.schedule import as_utc, format_lesson_at, utc_now

log = logging.getLogger(__name__)
router = Router()


def _selected_ids(markup: InlineKeyboardMarkup | None) -> set[int]:
    """Person ids currently ticked (✅) in a support-pick keyboard."""
    selected: set[int] = set()
    if markup is None:
        return selected
    for row in markup.inline_keyboard:
        for btn in row:
            data = btn.callback_data or ""
            if data.startswith("pick:") and btn.text.startswith("✅"):
                selected.add(int(data.split(":")[2]))
    return selected


def _has_pickmore(markup: InlineKeyboardMarkup | None) -> bool:
    """Есть ли в picker'е кнопка «➕ из пула коллег» (т.е. ещё не раскрыт)."""
    if markup is None:
        return False
    return any(
        (btn.callback_data or "").startswith("pickmore:")
        for row in markup.inline_keyboard for btn in row
    )


async def _pick_candidates(session, group, gender, call_id, *, expanded: bool):
    """(candidates, levels, can_expand) для picker'а: пул lead'а, при expanded —
    плюс пул коллег. Уже согласившиеся (claimed) исключены — иначе тап
    «раскрыть пул коллег» возвращает их обратно в список невыбранных, будто
    они ничего не подтверждали (фаза-2 фильтрует так же, см. scheduler.py)."""
    claimed_ids = await supports_svc.claimed_person_ids(session, call_id)
    lead = await groups_svc.resolve_recruit_lead(session, group)
    lead_id = lead.id if lead else None
    own_all = (
        await persons_svc.support_candidates(session, lead_id, gender, group.id, group.school_id)
        if lead_id is not None else []
    )
    own = [p for p in own_all if p.id not in claimed_ids]
    levels = (
        await persons_svc.admin_pool_levels(session, lead_id)
        if lead_id is not None else {}
    )
    colleagues_all = await persons_svc.school_pool_candidates(
        session, group.school_id, gender, group.id, {c.id for c in own},
        exclude_admin_id=lead_id,
    )
    colleagues = [p for p in colleagues_all if p.id not in claimed_ids]
    if expanded:
        return own + colleagues, levels, False
    return own, levels, bool(colleagues)


async def _call_context(session, call_id: int):
    """Returns (call, survey, group) for a SupportCall — any may be None."""
    call = await supports_svc.get_call(session, call_id)
    if call is None:
        return None, None, None
    survey = await surveys_svc.get_survey(session, call.survey_id)
    group = await groups_svc.get_group(session, survey.group_id) if survey else None
    return call, survey, group


def _balance_text(group, lesson_at, male: int, female: int,
                  claimed: int, needed: int, tz: str | None) -> str:
    """Текст recruit-сообщения lead'а с актуальным балансом (B8 живой бар)."""
    lines = [
        f"{ui.SUPPORT} <b>{escape(format_group_title(group))}</b> · "
        f"{format_lesson_at(lesson_at, '%d.%m', tz=tz)} {group.lesson_time}",
        "баланс пар:",
        ui.balance_bars(male, female),
    ]
    if needed > 0 and claimed >= needed:
        lines.append("Набор закрыт ✅")
    else:
        lines.append(f"откликнулись {claimed}/{needed}")
    return "\n".join(lines)


async def _refresh_balance(
    bot: Bot, *, picker_chat_id, picker_message_id, picker_has_keyboard: bool,
    group, lesson_at, gender, male: int, female: int, claimed: int, needed: int,
    tz: str | None,
) -> None:
    """B8: обновить баланс-строку сохранённого recruit-сообщения lead'а после
    claim/skip. Graceful: нет ref или сообщение удалено → молча выходим.

    picker_has_keyboard=True — сообщение несёт живой picker (кнопки выбора
    саппортов); edit_message_text без reply_markup снял бы их, поэтому такое
    сообщение не трогаем вовсе (баланс просто не обновится в нём вживую)."""
    if picker_chat_id is None or picker_message_id is None or picker_has_keyboard:
        return
    eff_male = male + (claimed if gender == Gender.male else 0)
    eff_female = female + (claimed if gender == Gender.female else 0)
    text = _balance_text(group, lesson_at, eff_male, eff_female, claimed, needed, tz)
    try:
        await bot.edit_message_text(
            text,
            chat_id=picker_chat_id,
            message_id=picker_message_id,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:  # noqa: BLE001
        log.debug("B8 balance refresh skipped (msg gone?): %s", e)


@router.callback_query(F.data.startswith("pick:"))
async def cb_support_pick(call: CallbackQuery) -> None:
    """Lead ticks/unticks a pool member in the recruit picker."""
    parts = (call.data or "").split(":")
    call_id, person_id = int(parts[1]), int(parts[2])
    markup = call.message.reply_markup if call.message else None
    selected = _selected_ids(markup)
    selected ^= {person_id}  # toggle
    expanded = not _has_pickmore(markup)  # пул коллег уже раскрыт?
    async with session_scope() as s:
        sc, _survey, group = await _call_context(s, call_id)
        if sc is None or group is None or group.school_id is None:
            await call.answer()
            return
        admin_id = await _admin_id_from_tg(call.from_user.id, s)
        if admin_id is None or not await groups_svc.can_manage_recruit(
            s, group.id, group.school_id, admin_id
        ):
            # picker открыт по callback'у; если юзер не lead — тихо игнор
            await call.answer()
            return
        candidates, levels, can_expand = await _pick_candidates(
            s, group, sc.gender, call_id, expanded=expanded
        )
    if call.message:
        await call.message.edit_reply_markup(
            reply_markup=support_pick_keyboard(
                call_id, candidates, selected, levels, can_expand=can_expand
            )
        )
    await call.answer()


@router.callback_query(F.data.startswith("pickmore:"))
async def cb_support_pickmore(call: CallbackQuery) -> None:
    """«➕ из пула коллег» — дораскрыть picker саппортами из пулов других преподов."""
    call_id = int((call.data or "").split(":")[1])
    selected = _selected_ids(call.message.reply_markup if call.message else None)
    async with session_scope() as s:
        sc, _survey, group = await _call_context(s, call_id)
        if sc is None or group is None or group.school_id is None:
            await call.answer()
            return
        admin_id = await _admin_id_from_tg(call.from_user.id, s)
        if admin_id is None or not await groups_svc.can_manage_recruit(
            s, group.id, group.school_id, admin_id
        ):
            await call.answer()
            return
        candidates, levels, _ = await _pick_candidates(
            s, group, sc.gender, call_id, expanded=True
        )
    if call.message:
        await call.message.edit_reply_markup(
            reply_markup=support_pick_keyboard(
                call_id, candidates, selected, levels, can_expand=False
            )
        )
    await call.answer()


async def _admin_id_from_tg(tg_id: int, session) -> int | None:
    from app.services import admins as admins_svc
    admin = await admins_svc.get_by_tg_id(session, tg_id)
    return admin.id if admin else None


@router.callback_query(F.data.startswith("picksend:"))
async def cb_support_pick_send(call: CallbackQuery, bot: Bot) -> None:
    """Admin шлёт recruit invites выбранным саппортам."""
    call_id = int((call.data or "").split(":")[1])
    selected = _selected_ids(call.message.reply_markup if call.message else None)
    if not selected:
        await call.answer("Сначала отметь, кого позвать", show_alert=True)
        return
    # Сериализуем status-переход одного call'а с dismiss/другим picksend: все они
    # гейтят на pending, но в разных сессиях — без общего lock'а гонка picksend↔
    # picknope дала бы «инвайты разосланы, но call закрыт как ‘баланс ок’».
    async with supports_svc.call_lock(call_id):
        async with session_scope() as s:
            sc, survey, group = await _call_context(s, call_id)
            if sc is None or survey is None or group is None or group.school_id is None:
                await call.answer("Уже обработано", show_alert=True)
                return
            admin_id = await _admin_id_from_tg(call.from_user.id, s)
            if admin_id is None or not await groups_svc.can_manage_recruit(
                s, group.id, group.school_id, admin_id
            ):
                await call.answer()
                return
            if sc.status != SupportCallStatus.pending:
                # Кто-то из ответственных уже разобрал (open/closed) — гасим дубль.
                await call.answer("Уже разобрано", show_alert=True)
                return
            if as_utc(survey.lesson_at) <= utc_now():
                # Занятие уже началось — рассылать «Нужна помощь!» поздно
                # (зеркало гарда в cb_support_join/cb_support_skip).
                await call.answer("Занятие уже началось — поздно звать", show_alert=True)
                return
            invited: list[tuple[int, str]] = []
            for pid in selected:
                p = await s.get(Person, pid)
                if p is not None:
                    invited.append((p.tg_user_id, p.full_name))
            if not invited:
                await call.answer("Никого не нашлось", show_alert=True)
                return
            # status → open + сброс таймера «тишины» на момент отправки инвайтов,
            # иначе «никто не откликнулся» отсчитывается от создания call'а (вечер).
            await supports_svc.mark_invited(s, call_id)
            title = format_group_title(group)
            place = await lesson_place(s, group, as_utc(survey.lesson_at))
            teachers_note = group.teachers_note
            lesson_time = group.lesson_time
            lesson_at = survey.lesson_at
            tz_name = (
                await schools_svc.get_timezone(s, group.school_id)
                if group.school_id else None
            )

    await send_support_invites(
        bot, call_id=call_id, invited=invited, title=title, place=place,
        teachers_note=teachers_note, lesson_time=lesson_time,
        lesson_at=lesson_at, tz=tz_name,
    )
    if call.message:
        names = ", ".join(n for _, n in invited)
        await call.message.edit_text(
            f"✅ Позвал: {names}.\nНапишу, как откликнутся."
        )
    await call.answer()


@router.callback_query(F.data.startswith("picknope:"))
async def cb_support_pick_nope(call: CallbackQuery) -> None:
    """Lead закрывает picker без добора — «в этот раз баланс ок». Graceful-выход
    у самого частого экрана добора: гасим call, чтобы pending-нудж не пинговал."""
    call_id = int((call.data or "").split(":")[1])
    async with supports_svc.call_lock(call_id):  # сериализация с picksend/другим dismiss
        async with session_scope() as s:
            sc, _survey, group = await _call_context(s, call_id)
            if sc is None or group is None or group.school_id is None:
                await call.answer()
                return
            admin_id = await _admin_id_from_tg(call.from_user.id, s)
            if admin_id is None or not await groups_svc.can_manage_recruit(
                s, group.id, group.school_id, admin_id
            ):
                await call.answer()
                return
            dismissed = await supports_svc.dismiss_call(s, call_id)
    if not dismissed:
        await call.answer("Уже разобрано", show_alert=True)
        return
    if call.message:
        await call.message.edit_text(
            "Ок, в этот раз добор не нужен — баланс сложился. "
            "Если баланс снова перекосит — позову сам."
        )
    await call.answer()


@router.callback_query(F.data.startswith("sup:join:"))
async def cb_support_join(call: CallbackQuery, bot: Bot) -> None:
    call_id = int((call.data or "").split(":")[2])
    # Сериализуем весь read→claim→commit одного call'а: два одновременных тапа на
    # последний слот иначе прочитают stale count и перебрали бы набор. Lock должен
    # охватывать session_scope целиком — commit (видимость вставки) на его __aexit__.
    async with supports_svc.call_lock(call_id):
        async with session_scope() as s:
            sc = await supports_svc.get_call(s, call_id)
            if sc is None:
                await call.answer()
                return
            survey = await surveys_svc.get_survey(s, sc.survey_id)
            group = await groups_svc.get_group(s, survey.group_id) if survey else None
            if group is None or group.school_id is None:
                await call.answer()
                return
            if as_utc(survey.lesson_at) <= utc_now():
                # Занятие уже началось/прошло — поздний тап не засчитываем.
                if call.message:
                    await call.message.edit_text(
                        "Занятие уже началось — добор закрыт. Спасибо, что откликнулся!"
                    )
                await call.answer()
                return
            if sc.status not in (SupportCallStatus.pending, SupportCallStatus.open):
                # Заполнен elsewhere либо lead дал отбой (dismiss) — новый отклик
                # не принимаем, claim() отказал бы тем же гардом.
                if call.message:
                    await call.message.edit_text(
                        "Добор уже закрыт, спасибо, что откликнулся!"
                    )
                await call.answer()
                return
            person = await persons_svc.get_person(s, group.school_id, call.from_user.id)
            if person is None:
                await call.answer("Тебя нет в этой школе", show_alert=True)
                return
            accepted, filled, needed = await supports_svc.claim(s, call_id, person.id)
            notify_tgs = await groups_svc.group_admin_tg_ids(s, group.id, group.school_id)
            person_name = person.full_name
            title = format_group_title(group)
            group_name = group.name
            lesson_at = survey.lesson_at
            tz_name = await schools_svc.get_timezone(s, group.school_id)
            # B8: данные для живого баланс-бара в recruit-сообщении lead'а.
            picker_chat_id = sc.picker_chat_id
            picker_message_id = sc.picker_message_id
            picker_has_keyboard = sc.picker_has_keyboard
            call_gender = sc.gender
            call_needed = sc.needed
            male_yes, female_yes = await surveys_svc.gender_yes_counts(s, sc.survey_id)
            claimed_total = await supports_svc.count_claims(s, call_id)

    when = format_lesson_at(lesson_at, tz=tz_name)
    if not accepted:
        existing = await _is_already_claimed(call_id, person.id)
        if existing:
            if call.message:
                await call.message.edit_text(
                    f"✅ Ты записан саппортом на «{title}» {when} — "
                    f"ждём тебя на занятии.",
                    reply_markup=support_flip_keyboard(call_id, "join"),
                )
            await call.answer()
            return
        if call.message:
            await call.message.edit_text(
                f"Уже набрали нужное число на «{title}» {when} — "
                f"спасибо, что откликнулся!"
            )
        await call.answer()
        return

    if call.message:
        await call.message.edit_text(
            f"✅ Спасибо! Ты записан саппортом на «{title}» {when} — "
            f"ждём тебя на занятии.",
            reply_markup=support_flip_keyboard(call_id, "join"),
        )
    await call.answer("Записал!")
    done = "  Набор закрыт ✅" if filled >= needed else ""
    for tg in notify_tgs:
        try:
            await bot.send_message(
                tg,
                f"🆘 {person_name} придёт выручить «{group_name}» — "
                f"{filled}/{needed}.{done}",
            )
        except Exception:  # noqa: BLE001
            pass
    await _refresh_balance(
        bot, picker_chat_id=picker_chat_id, picker_message_id=picker_message_id,
        picker_has_keyboard=picker_has_keyboard, group=group, lesson_at=lesson_at,
        gender=call_gender, male=male_yes, female=female_yes, claimed=claimed_total,
        needed=call_needed, tz=tz_name,
    )


@router.callback_query(F.data.startswith("sup:skip:"))
async def cb_support_skip(call: CallbackQuery, bot: Bot) -> None:
    call_id = int((call.data or "").split(":")[2])
    reverted_name: str | None = None
    filled = needed = 0
    notify_tgs: list[int] = []
    title = "занятие"
    when = ""
    group_name = ""
    lesson_over = False
    # B8: состояние для живого баланс-бара.
    picker_chat_id = picker_message_id = None
    picker_has_keyboard = False
    call_gender = None
    male_yes = female_yes = 0
    lesson_at_val = None
    group_obj = None
    tz_name: str | None = None
    # unclaim переоткрывает closed-call по snapshot-статусу — сериализуем с join
    # тем же call_lock'ом, иначе конкурентный join (fill→closed) закроет call, а
    # skip прочитает stale open и не переоткроет освобождённый слот.
    async with supports_svc.call_lock(call_id):
        async with session_scope() as s:
            sc = await supports_svc.get_call(s, call_id)
            survey = await surveys_svc.get_survey(s, sc.survey_id) if sc else None
            group = (
                await groups_svc.get_group(s, survey.group_id) if survey else None
            )
            if group is not None and survey is not None and group.school_id is not None:
                title = format_group_title(group)
                tz_name = await schools_svc.get_timezone(s, group.school_id)
                when = format_lesson_at(survey.lesson_at, tz=tz_name)
                group_name = group.name
                if as_utc(survey.lesson_at) <= utc_now():
                    # Занятие уже началось/прошло — поздний «не смогу» не должен
                    # стирать явку задним числом (зеркало гарда в cb_support_join).
                    lesson_over = True
                else:
                    person = await persons_svc.get_person(
                        s, group.school_id, call.from_user.id
                    )
                    if person is not None and sc is not None:
                        if await supports_svc.unclaim(
                            s, call_id, person.id, now=utc_now()
                        ):
                            reverted_name = person.full_name
                            filled = await supports_svc.count_claims(s, call_id)
                            needed = sc.needed
                            notify_tgs = await groups_svc.group_admin_tg_ids(
                                s, group.id, group.school_id
                            )
                            picker_chat_id = sc.picker_chat_id
                            picker_message_id = sc.picker_message_id
                            picker_has_keyboard = sc.picker_has_keyboard
                            call_gender = sc.gender
                            male_yes, female_yes = await surveys_svc.gender_yes_counts(
                                s, sc.survey_id
                            )
                            lesson_at_val = survey.lesson_at
                            group_obj = group

    if call.message:
        # when может быть пустым (survey/group не нашлись) — вставляем через
        # join, а не f-string с фиксированным пробелом, иначе «занятие»  не —
        # двойной пробел.
        when_part = f" {when}" if when else ""
        if lesson_over:
            text = f"Занятие «{title}»{when_part} уже прошло — спасибо, что откликнулся!"
        else:
            text = f"Понял, на «{title}»{when_part} не сможешь. Спасибо, что откликнулся!"
        await call.message.edit_text(
            text, reply_markup=support_flip_keyboard(call_id, "skip")
        )
    await call.answer()

    if reverted_name is not None:
        for tg in notify_tgs:
            try:
                await bot.send_message(
                    tg,
                    f"↩️ {reverted_name} передумал — снова «{group_name}» "
                    f"{filled}/{needed}.",
                )
            except Exception:  # noqa: BLE001
                pass
        await _refresh_balance(
            bot, picker_chat_id=picker_chat_id, picker_message_id=picker_message_id,
            picker_has_keyboard=picker_has_keyboard, group=group_obj,
            lesson_at=lesson_at_val, gender=call_gender, male=male_yes,
            female=female_yes, claimed=filled, needed=needed, tz=tz_name,
        )


async def _is_already_claimed(call_id: int, person_id: int) -> bool:
    async with session_scope() as s:
        from sqlalchemy import select

        from app.db.models import SupportClaim

        row = (
            await s.execute(
                select(SupportClaim).where(
                    SupportClaim.support_call_id == call_id,
                    SupportClaim.person_id == person_id,
                )
            )
        ).scalar_one_or_none()
        return row is not None
