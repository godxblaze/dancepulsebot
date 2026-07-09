# Чек-лист дня 0 — деплой пилота

Команды копируются как есть, запускать из корня проекта в PowerShell от администратора
(регистрация Task Scheduler-задач требует прав администратора).

## 1. Task Scheduler — автозапуск + бэкап + watchdog

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_bot_task.ps1
powershell -ExecutionPolicy Bypass -File scripts/install_backup_task.ps1
powershell -ExecutionPolicy Bypass -File scripts/install_watchdog_task.ps1
```

Проверка:

```powershell
Get-ScheduledTaskInfo -TaskName "DancePulseBot"
Get-ScheduledTaskInfo -TaskName "DancePulseBackup"
Get-ScheduledTaskInfo -TaskName "DancePulseWatchdog"
```

Все три задачи зарегистрированы под принципалом SYSTEM (`ServiceAccount`) — стартуют
без логина пользователя. Запустить бота сейчас: `Start-ScheduledTask -TaskName "DancePulseBot"`.

`DancePulseWatchdog` — рестартует `DancePulseBot`, если event loop завис
(процесс жив, но heartbeat в `logs/heartbeat` не обновлялся >10 мин) или
задача не в состоянии `Running`; шлёт алерт первому id из `DEVELOPER_TG_IDS` напрямую через
Telegram Bot API (работает, даже если сам бот не поднимается). Проверка после
установки: `Get-ScheduledTaskInfo -TaskName "DancePulseWatchdog"` — `LastRunTime`
должен обновляться каждые 5 минут.

## 2. Свежая боевая БД

Дев-`dance.sqlite3` испорчена старым TZ-багом (-3ч) — **не переносить на пилот**.
Свежая БД создаётся автоматически при первом запуске (`init_db()`).

В `.env` прописать:

```
BOT_TOKEN=<токен от @BotFather>
DEVELOPER_TG_IDS=<твой Telegram user_id, через запятую если несколько>
OWNER_CLAIM_SECRET=<случайная строка — секрет для /claim>
DEVELOPER_CONTACT=<@username или ссылка — куда слать нераспознанных>
```

`OWNER_CLAIM_SECRET` даёт преподу самостоятельный онбординг через `/claim <secret>`
без ручного `/grant`. Менять/чистить секрет между пилотами.

## 3. Очистка + префлайт

```powershell
python -m scripts.purge_fakes
python -m scripts.check_pilot
```

`check_pilot` — только чтение; ноль замечаний перед стартом с реальной школой.

## 4. Offsite-копия backups/

`backups/` содержит снапшоты БД (pg_dump, включает FSM-состояние — таблица
`fsm_state`) + копии `.env` (с токеном бота!).
Offsite — **только в приватное хранилище** (личный облачный клиент/robocopy-задача
в приватную папку), не в публичный/шаренный репозиторий.

## 5. Проверка двойного запуска

```powershell
python -m app.main
# в другом окне, не останавливая первый:
python -m app.main
```

Второй процесс должен упасть с сообщением о занятом lock-файле (`dance.sqlite3.lock`)
и ненулевым кодом выхода (`echo $LASTEXITCODE`).

## 6. Ребут-проверка + ежедневная рутина недели 1

- `shutdown /r` — после ребута **не логиниться**; с телефона убедиться, что бот
  отвечает; `Get-ScheduledTaskInfo -TaskName "DancePulseBot"` показывает `LastRunTime`
  сразу после старта системы.
- Ежедневно неделю 1: `logs/dance.log` на `ERROR`/`Traceback`; свежесть файла в
  `backups/` (не старше суток); `/status` в боте (owner-команда).
