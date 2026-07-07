# Разработка

Заметки для разработчика: деплой пилота, детали тестов, тулинг качества. Продуктовый обзор — [README.md](README.md).

## Запуск пилота и перенос на сервер

Минимальный список действий, чтобы запустить с реальными данными и потом без боли мигрировать.

### Гейты пилота

- ~~Co-admin-инвайты — не выдавать до закрытия co-admin-пачки.~~ **Закрыто
  2026-07-06.** Все 5 дефектов пачки почищены: `/schrem` достижим из UI (карточка
  школы → «👥 Состав школы»), `remove_coadmin` чистит `recruit_lead_admin_id` и
  `GroupAdmin` снятого, мастер новой группы больше не заводит фантомную «Моя
  школа» co-admin'у (гвард в `/newgroup` + `school_id` в getter'ах new_group.py),
  support-ссылка co-admin'а без owned-школы падает на школу-членство вместо
  «не найдена», `/export`/статистика берут `primary_school_id` (owned, иначе
  первая школа-членство) вместо строго owned. Второго препода приглашать можно.
- ~~Режим добора на пилоте — только `curated`.~~ **Закрыто 2026-07-06.** `escalate`
  чинён: фильтр уже согласившихся (не дублирует инвайт) плюс grace-период
  (`ESCALATE_MIN_GRACE`, scheduler.py) — свежий picker, переоткрытый фазой 2
  ближе к занятию, чем `ESCALATE_LEAD`, больше не эскалирует на первом же тике.
  Все 4 режима добора доступны на пилоте.
- ~~Scheduler-side `call_lock`.~~ **Закрыто 2026-07-06.** `_maybe_recruit`
  (escalate-переход и фаза-2 добора остатка) теперь берёт `supports_svc.call_lock`
  так же, как все tap-хендлеры — свежий re-fetch call внутри лока вместо
  устаревших значений. Гонка scheduler-тик ↔ конкурентный тап («Не сейчас» в то
  же мгновение, что эскалация) закрыта и покрыта тестом на конкурентность.
- ~~Полная заморозка идентичности занятия.~~ **Практический разрыв закрыт
  2026-07-06** (архитектурный источник — lesson_at пересчитывается на лету из
  мутируемых Group-полей — остаётся как есть, это не переделывали).
  `_maybe_recruit` теперь проверяет `survey.status == open` и не ведёт добор для
  осиротевшего survey; `close_orphaned_survey` при правке расписания/TZ гасит и
  привязанный SupportCall, а не только сам Survey. **Известный узкий остаточный
  риск**: закрытие call'а в `close_orphaned_survey` не сериализовано
  `call_lock` (см. докстринг функции) — обернуть в отдельную залоченную
  транзакцию нельзя, упирается в SQLite single-writer lock уже открытой
  вызывающей транзакции (`database is locked`). Правильный фикс требует
  разносить commit закрытия Survey и commit закрытия SupportCall по разным
  транзакциям на уровне хендлеров (edit_group.py/school.py) — рефакторинг
  большего масштаба, отложен осознанно (окно — миллисекунды, триггер — админ
  меняет расписание в тот же миг, что саппорт тапает join/dismiss).
- ~~429/TelegramRetryAfter-троттлинг рассылок.~~ **Закрыто 2026-07-06.**
  `send_with_retry` (app/bot/utils.py) ловит 429, спит `retry_after`, повторяет
  один раз — применено во всех циклах рассылки scheduler.py и lesson_cancel.py.
  Не превентивный троттлинг (пауз между сообщениями по-прежнему нет), а
  реактивный — этого достаточно на объёме одной школы; при росте пилота до
  нескольких школ пересмотреть.
- Полный аудит — [docs/PILOT_AUDIT_2026-07-02.md](docs/PILOT_AUDIT_2026-07-02.md);
  чек-лист дня деплоя — [docs/PILOT_DAY0_CHECKLIST.md](docs/PILOT_DAY0_CHECKLIST.md).

**Перед стартом** (один раз):

```powershell
# .env: BOT_TOKEN, DEVELOPER_ID (твой Telegram user_id). Без них стартовать запретит.
# Для самостоятельного онбординга препода — OWNER_CLAIM_SECRET (см. ниже).
# Опционально снести dev-фейков, если уже наполняли базу демо-данными:
python -m scripts.purge_fakes --dry-run   # посмотреть кого тронем
python -m scripts.purge_fakes             # реально удалить

# Префлайт перед пилотом — ловит тихие поломки (люди без пола в пуле/ответах,
# группы без дней недели/куратора, кривой timezone). Только чтение:
python -m scripts.check_pilot
```

**Онбординг первого преподавателя.** Чтобы владелец школы получил доступ без
ручного `/grant`, задай в `.env` `OWNER_CLAIM_SECRET=<секрет>` и передай ему:
«напиши боту `/claim <секрет>`». После этого он жмёт `/start` и называет школу.
Для красивого приветствия нераспознанному пользователю можно задать
`DEVELOPER_CONTACT=@твой_ник`. Альтернатива на одного-двух — старый путь:
препод присылает свой `/myid`, ты делаешь `/grant <id>`.

**Каждый день делай бэкап:**

```powershell
python -m scripts.backup_db   # кладёт в backups/dance-YYYYMMDD-HHMMSS.sqlite3
```

Подключи к Task Scheduler / cron, чтобы крутилось автоматически. Хранит 14 дней,
но всегда оставляет минимум 7 последних копий (даже при долгом простое).

На Windows-хосте бэкап ставится одной командой (см. [scripts/install_backup_task.ps1](scripts/install_backup_task.ps1)):

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_backup_task.ps1
# ежедневно в 03:00 → python -m scripts.backup_db
```

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
Restart=always
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

`Restart=always` поднимает бота после любого падения, OOM или перезагрузки —
без этого процесс, упавший ночью, останется лежать до утра. На старте бот сам
снимает залётный webhook и, если тот же токен уже запущен где-то ещё, пишет в лог
и шлёт разработчику предупреждение о конфликте polling (иначе бот «молча мёртв»).

**Windows-хост (пилот без VPS).** Те же гарантии даёт Task Scheduler — готовый
установщик [scripts/install_bot_task.ps1](scripts/install_bot_task.ps1) (запускать
от администратора):

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_bot_task.ps1
# автозапуск при старте системы + рестарт каждую минуту при падении
Start-ScheduledTask -TaskName DancePulseBot   # поднять сейчас
```

**Несколько админов на одну школу.** Первый owner получает доступ через `/claim <секрет>` (или `/grant` от разработчика — см. «Онбординг первого преподавателя» выше), затем при `/start` называет школу. Чтобы дать второму преподавателю co-admin-доступ, owner идёт в `/school` → «Пригласить co-admin'а» и пересылает кандидату одноразовую ссылку с TTL. Кандидат открывает её в боте и сразу становится co-admin'ом школы — никаких dev-скриптов.

После этого co-admin видит все группы школы в `/groups`, получает auto-summary, может пинговать молчащих и вести пул. Удалить школу, менять настройки автодобора и кикать других членов может только owner.

## Тесты (детали)

```powershell
pytest tests/
```

352 теста. Файлы (основные):
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
- `test_recruit_modes.py` / `test_recruit_edge.py` — режимы добора (curated/open_race/auto_top/escalate) + edge-кейсы (пустой пул, отмена, смена стороны, идемпотентность).
- `test_pool_recruit_mode.py` — UI выбора режима в `/pool`.
- `test_quick_wins.py` — соц-доказательство, дедлайн, commitment-копирайт.
- `test_reactivation.py` — реактивация молчунов + throttle.
- `test_balance_bar.py` — живой баланс-бар при claim/skip.
- `test_e2e_*.py` — сквозные сценарии поверх FakeBot.

Тесты с БД используют временную базу через [conftest.py](tests/conftest.py).

### E2E — настоящее прокликивание живого бота

`tests/` гоняют хендлеры через `FakeBot` (быстро, без сети). Чтобы реально
прокликать `@dancepulsebot` через настоящий Telegram — каталог [e2e/](e2e/):

- **[e2e/live/](e2e/live/)** — Telethon: user-аккаунт по MTProto шлёт боту сообщения
  и жмёт inline-кнопки программно. Headless, повторяемый suite. `pip install -e ".[dev,e2e]"`,
  одноразовый `python -m e2e.live.login`, затем `pytest e2e/live -v`.
- **[e2e/web/](e2e/web/)** — Playwright MCP: клики в видимом web.telegram.org + скриншоты,
  для визуальных спот-чеков (нужен Node.js).

Оба — отдельным Telegram-аккаунтом-«тестером». Не подхватываются обычным `pytest` и
Stop-hook'ом. Подробности и пререквизиты — [e2e/README.md](e2e/README.md).

## Контроль качества

Проект настроен на автоматическую проверку при разработке через Claude Code:

- **Stop-hook** ([.claude/settings.json](.claude/settings.json)) — перед завершением каждого ответа гоняется `ruff` + `pytest` ([scripts/check.ps1](scripts/check.ps1)); красные проверки блокируют завершение.
- **Субагент-ревьюер** ([.claude/agents/reviewer.md](.claude/agents/reviewer.md)) — независимо проверяет крупные куски кода против требований и принципов проекта.

Прогнать проверку вручную: `powershell -File scripts/check.ps1`.
