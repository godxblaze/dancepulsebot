# Dance Pulse

Telegram-бот для танцевальных школ. Делает две вещи, которых нет в коробочных CRM:

1. **Опрос посещаемости** — перед каждым занятием спрашивает учеников «придёшь?» и даёт преподавателю готовую сводку.
2. **Автосбор саппортов** (киллер-фича) — ловит гендерный перекос в парах лид/фолловер и сам добирает помощников из пула.

Это **не CRM**. Учёт абонементов, платежей, расписания залов — у школ для этого уже есть коробочные продукты (AppEvent, Impulse CRM и т.п.). Бот сознательно делает только то, чего эти CRM не делают.

План проекта: `C:\Users\user1\.claude\plans\elegant-dreaming-rabin.md`.

## Что работает

**Преподаватель** — команды `/start`, `/newgroup`, `/groups`, `/today`, `/stats`, `/pool`, `/help`.

- **Создание группы** — мастер `/newgroup` на aiogram-dialog: направление → уровень → дни недели → время → площадка → зал → ведущие. Кнопка «◀ Назад» на каждом шаге. Сразу после создания бот шлёт картинку-приглашение: QR + ссылка одним сообщением, готовое к пересылке.
- **Карточка группы** (`/groups`) — кнопки в две колонки: «👥 Ученики», «📨 Пригласить», «📢 Сообщение», «✏️ Изменить», «🚫 Отменить занятие», «✏️ Отметить посещаемость», «⏸ Пауза», «🗑 Удалить» (мёртвые кнопки скрываются автоматически).
- **Редактор группы** — тоже dialog: меню с 7 пунктами по полям. Любое поле меняется тапом, «◀ Назад» возвращает в меню.
- **Карточка ученика** — тап по имени в списке группы: пол, уровень в пуле, в каких ещё группах. Действия: переключить уровень / пол, убрать из группы или (если walk-in) — «➕ Записать постоянно». Все toggle-кнопки идемпотентны: двойной тап не перескакивает.
- **Опросы посещаемости** — за `lead_hours` до занятия бот рассылает ученикам «придёшь?» с кнопками да/нет; после ответа — одна противоположная flip-кнопка. `/today` — сводка ближайших занятий с переключателем «Сегодня/завтра ↔ На неделю» и кнопками пинга молчащим. На середине окна преподу уходит авто-сводка с пингом и ссылкой на карточку группы.
- **Auto-reminder** — за 4 ч и 1 ч до занятия молчащим уходит отдельное «⏰ Напоминаю» (идемпотентно через `Response.reminded_4h/_1h`).
- **Broadcast в группу** — сообщение с preview-confirm перед рассылкой (отдельный dialog).
- **Post-lesson отметка** — препод выбирает прошедшее занятие → ростер с toggle (☐→✅→❌→☐) + walk-in для разовых гостей (без membership). Постранично по 10.
- **`/stats`** — посещаемость по группам за последние занятия: «обещали M → пришли N (no-show: K)», средний отклик и реальная посещаемость отдельно.
- **`/school`** — карточка школы: состав, ⚙ настройки автодобора (порог перекоса 1-5, отсечка 2-24 ч), приглашение co-admin'а одноразовой ссылкой с TTL, переименование/удаление/выход.

**Ученик** — никаких команд, всё кнопками:

- **Онбординг** — переходит по QR-коду или ссылке → бот спрашивает имя (кнопкой с Telegram-именем или вводом) и пол → запись в группу, преподу уведомление с кнопкой «👤 Открыть карточку».
- Отвечает на опрос одной кнопкой; передумал — ещё одной (противоположной).

**Автосбор саппортов:**

- На отсечке (настраивается, default 6 ч) бот считает баланс парней/девушек среди ответивших «приду».
- Если перекос ≥ порога (default 2) — присылает преподу сводку и список его пула нужного пола с галочками. Препод тапает, кого позвать, и жмёт «Позвать».
- Бот пишет выбранным «нужна помощь, придёшь?». Первые N согласившихся занимают места; преподу — живые уведомления; у саппорта растёт счётчик «помог N раз».
- `/pool` — преподаватель ведёт личный пул, тапом задаёт уровень навыка 1→2→3→убрать (приоритет в picker'е: 3 показывается первым). Постранично по 10.
- **Настройки порога и отсечки** — в карточке школы → ⚙ Настройки автодобора (owner-only).

Фидбек-опросы после занятия — отложены (потребность не подтверждена; схема в БД есть, код не пишется).

## Архитектура

```
┌──────────────┐   polling    ┌─────────────────────────────────────┐
│ Telegram     │ ───────────▶ │  aiogram 3 + aiogram-dialog         │
│ препод+учен  │ ◀── reply ── │  handlers/* (по фичам)              │
└──────────────┘              │  dialogs/* (wizard'ы: NewGroup,     │
                              │  EditGroup, Broadcast, …)           │
                  ┌───────────┴───────────────────────┼─────────────┘
                  ▼                                   ▼
          ┌──────────────┐                  ┌──────────────┐
          │ APScheduler  │                  │  SQLAlchemy  │
          │ tick / 15мин │                  │   (SQLite)   │
          └──────────────┘                  └──────────────┘
```

Бот и scheduler — в одном процессе ([app/main.py](app/main.py)). Бизнес-логика в [app/services/](app/services/) переиспользуется хэндлерами и scheduler'ом. Все wizard'ы и multi-step edit-flow — на [aiogram-dialog](https://github.com/Tishka17/aiogram_dialog), короткие message-handler'ы и колбэки — на чистом aiogram FSM.

**Модель данных:** `School` — top-level контейнер с членством через `SchoolMember(role)`. `Person` — человек в школе (имя, пол, флаги пула, счётчик помощи). `Membership` — связь person↔group. `Survey`/`Response` — опрос на занятие и ответы. `SupportCall`/`SupportClaim` — автодобор саппортов. `LessonCancellation` — отмена конкретного занятия.

## Структура

```
dancepulsebot/
├── app/
│   ├── core/        # config, logging
│   ├── db/          # модели SQLAlchemy, session (+ PRAGMA foreign_keys, lightweight migrations)
│   ├── bot/
│   │   ├── bot.py            # build_bot / build_dispatcher (роутеры + setup_dialogs)
│   │   ├── keyboards.py      # inline и reply-меню + пагинация
│   │   ├── states.py         # FSM (JoinFlow, CancelLesson) — короткие
│   │   ├── utils.py          # QR, имя бота, format helpers
│   │   ├── commands.py       # set_my_commands per-scope
│   │   ├── fsm_storage.py    # JSON-storage FSM (переживает рестарт)
│   │   ├── dialogs/          # aiogram-dialog wizards и FSM на стероидах
│   │   │   ├── states.py     # все SG для dialog'ов
│   │   │   ├── new_group.py  # мастер /newgroup (9 окон)
│   │   │   ├── edit_group.py # редактор группы (menu + 10 sub-окон)
│   │   │   └── misc_dialogs.py # CreateSchool, RenameSchool, EditName, Broadcast
│   │   └── handlers/         # фича-роутеры
│   │       ├── _access.py    # shared: accessible_group / owned_group / demote
│   │       ├── _callbacks.py # parse_cb helper
│   │       ├── admin.py      # /start /help /grant /today /stats /groups + person card
│   │       ├── student.py    # /start с deep-link join, JoinFlow
│   │       ├── survey.py     # ответы «приду / не приду» (att:*)
│   │       ├── recruit.py    # picker + invite саппортов
│   │       ├── school.py     # карточка школы, настройки автодобора, partner-invite
│   │       ├── pool.py       # /pool с пагинацией и idempotency-guard
│   │       ├── attendance.py # post-lesson отметка + walk-in
│   │       └── lesson_cancel.py # 3-шаговая отмена с опц. причиной
│   ├── scheduler/   # scheduler.py — tick: опросы, авто-сводки, auto-reminders (4h/1h), рекрутинг, stale support pings
│   ├── services/    # admins, schools, groups, persons, venues, surveys, supports, cancellations, exports, schedule
│   └── main.py
├── scripts/         # demo_seed, demo_reset, backup_db, restore_db, purge_fakes, grant_coadmin
└── tests/           # 116 тестов, pytest + ruff гонятся через scripts/check.ps1
```

## Требования

- Python 3.12+
- Telegram-бот-токен от [@BotFather](https://t.me/BotFather)

База данных — SQLite, отдельный сервер не нужен; файл создаётся автоматически.

## Запуск

```powershell
# 1. Окружение и зависимости
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 2. Настроить .env
copy .env.example .env
# вписать BOT_TOKEN от @BotFather и свой DEVELOPER_ID (Telegram user_id)

# 3. Запустить
python -m app.main
```

SQLite-файл (`dance.sqlite3`) создаётся при первом старте. Логи пишутся в [logs/dance.log](logs/) (rotating, 5 MB × 5 backups).

Проверка с двух Telegram-аккаунтов:
1. Препод: `/start` → `/newgroup` → пройти мастер. Группа создана — есть ссылка и QR-код.
2. Ученик (второй аккаунт): перейти по ссылке → ответить на вопросы про имя и пол.
3. Препод: `/groups` → группа → «Ученики» — ученик в списке; преподу пришло уведомление о записи.
4. Опрос: в `/newgroup` задай время занятия в пределах `DEFAULT_LEAD_HOURS` часов — бот в течение 15 минут разошлёт опрос. Ответь кнопкой, проверь `/today`.

## Запуск пилота и перенос на сервер

Минимальный список действий, чтобы запустить с реальными данными и потом без боли мигрировать.

**Перед стартом** (один раз):

```powershell
# .env: BOT_TOKEN, DEVELOPER_ID (твой Telegram user_id). Без них стартовать запретит.
# Опционально снести dev-фейков, если уже играли с seed_fake.sql:
python -m scripts.purge_fakes --dry-run   # посмотреть кого тронем
python -m scripts.purge_fakes             # реально удалить
```

**Каждый день делай бэкап:**

```powershell
python -m scripts.backup_db   # кладёт в backups/dance-YYYYMMDD-HHMMSS.sqlite3
```

Подключи к Task Scheduler / cron, чтобы крутилось автоматически. Хранит 14 дней.

**Что вообще «является базой»** (что нужно копировать при переносе):
- `dance.sqlite3` — собственно данные.
- `dance.sqlite3-wal`, `dance.sqlite3-shm` — журнал WAL. Существуют если бот запущен. **Перед переносом останови бота**, чтобы WAL слился в основной файл.
- `dance.fsm.json` — состояние мастеров создания группы и онбординга ученика. Если не успели завершить — без него потеряются. На несколько пользователей это не критично.
- `.env` — токен и DEVELOPER_ID. **Не клади в git и не таскай как файл — перенеси значения вручную.**

**Перенос на VPS Linux:**

```bash
# 1. на старом хосте: остановить бота (Ctrl+C / завершить процесс) и сделать снапшот
python -m scripts.backup_db

# 2. перенести два файла на сервер (dance.fsm.json — по желанию):
scp backups/dance-YYYYMMDD-HHMMSS.sqlite3 user@server:/srv/dancepulse/
scp dance.fsm.json user@server:/srv/dancepulse/    # опционально

# 3. на сервере: восстановить под именем dance.sqlite3
ssh user@server
cd /srv/dancepulse
python -m scripts.restore_db dance-YYYYMMDD-HHMMSS.sqlite3
# скрипт сделает страховочный snapshot текущей базы (если была), уберёт WAL/shm,
# скопирует backup в dance.sqlite3 и прогонит PRAGMA integrity_check.

# 4. .env заполнить вручную, запустить
python -m app.main
```

Под systemd на сервере:

```ini
# /etc/systemd/system/dancepulse.service
[Unit]
Description=Dance Pulse bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/srv/dancepulse
EnvironmentFile=/srv/dancepulse/.env
ExecStart=/srv/dancepulse/.venv/bin/python -m app.main
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now dancepulse
journalctl -u dancepulse -f       # хвост логов
```

Бэкап через cron:

```cron
0 3 * * *  cd /srv/dancepulse && /srv/dancepulse/.venv/bin/python -m scripts.backup_db >> logs/backup.log 2>&1
```

**Несколько админов на одну базу.** Owner-админ группы добавляется автоматически при `/newgroup`. Чтобы дать второму преподавателю co-admin-доступ:

```powershell
# Сначала кандидат должен написать боту /start, чтобы у него был aiogram-чат.
python -m scripts.grant_coadmin <group_id> <tg_user_id> "Имя"
```

После этого второй админ увидит группу в `/groups`, будет получать auto-summary, сможет пинговать молчащих. Удалить группу или поменять её настройки сможет только owner.

## Тесты

```powershell
pytest tests/
```

116 тестов (FSM-тесты NewGroup/EditGroup удалены после миграции на aiogram-dialog — UI-фреймворк тестируется upstream'ом, бизнес-логика покрывается в `test_services.py`). Файлы:
- `test_schedule.py` — расчёт следующего занятия (граничные случаи).
- `test_smoke.py` — без БД (генерация join-кодов).
- `test_surveys.py` — ядро опросов: создание, ответы, сводка, закрытие.
- `test_services.py` — CRUD админов / групп / людей, каскадное удаление.
- `test_scheduler.py` — `tick`: рассылка опросов и авто-сводок, окно `lead_hours`, идемпотентность.
- `test_handlers.py` — ответ на опрос посещаемости.
- `test_onboarding.py` — запись по QR/ссылке: пол, имя, генерация QR.
- `test_admin_handlers.py` — `/today`, `/stats`, мастер группы, карточка группы.
- `test_pools.py` — пулы саппортов: самозапись и личный пул админа.
- `test_recruit.py` — автосбор: дефицит, подтверждение, рассылка, отклики.
- `test_e2e_*.py` — сквозные сценарии поверх FakeBot.

Тесты с БД используют временную базу через [conftest.py](tests/conftest.py).

## Контроль качества

Проект настроен на автоматическую проверку при разработке через Claude Code:

- **Stop-hook** ([.claude/settings.json](.claude/settings.json)) — перед завершением каждого ответа гоняется `ruff` + `pytest` ([scripts/check.ps1](scripts/check.ps1)); красные проверки блокируют завершение.
- **Субагент-ревьюер** ([.claude/agents/reviewer.md](.claude/agents/reviewer.md)) — независимо проверяет крупные куски кода против требований и принципов проекта.

Прогнать проверку вручную: `powershell -File scripts/check.ps1`.

## Roadmap

- **Пилот** — обкатать автосбор саппортов на реальной школе.
- **Доработки по пилоту** — UI для приглашения co-admin без dev-скрипта, фора личного пула по времени, экспорт статистики, учёт надёжности саппортов, отмена занятия одной кнопкой.
- **Рост** — уровень «школа» над админом, когда появится спрос (см. план).

## Решения дизайна

- **SQLite, не Postgres.** Утилите не нужна инфраструктура — один файл.
- **Управление в боте, без веб-панели.** Преподаватель в телефоне.
- **Polling, не webhook.** Не нужен публичный URL.
- **Бот в личке, не в групповом чате.** Автосбор саппортов невозможен на нативных опросах Telegram — нужен поимённый ростер с ролями и личные сообщения подмножеству людей. Это и есть то, что отличает бота от ручной переклички.
- **Один `Person`, а не отдельные «ученики» и «саппорты».** Человек может быть и тем и другим одновременно — это флаги, а не разные сущности.
- **Не дублирует школьную CRM.** Бот знает людей только чтобы слать опрос и собирать саппортов, он не «система записи».
```
