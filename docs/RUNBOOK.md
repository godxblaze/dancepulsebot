# Ops runbook — запуск, мониторинг, перенос

Короткая шпаргалка на каждый день. Полный чек-лист первого разворачивания —
[PILOT_DAY0_CHECKLIST.md](PILOT_DAY0_CHECKLIST.md).

## Запуск (один раз, PowerShell от администратора)

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_bot_task.ps1
powershell -ExecutionPolicy Bypass -File scripts/install_backup_task.ps1
powershell -ExecutionPolicy Bypass -File scripts/install_watchdog_task.ps1
Start-ScheduledTask -TaskName "DancePulseBot"
```

Три задачи Task Scheduler под SYSTEM (переживают ребут/логаут):

| Задача | Что | Когда |
|---|---|---|
| `DancePulseBot` | сам бот | автостарт при загрузке, рестарт при падении |
| `DancePulseBackup` | снапшот БД+`.env` в `backups/` | ежедневно 03:00 |
| `DancePulseWatchdog` | чинит зависание (процесс жив, но не отвечает) | каждые 5 мин |

Дальше руками ничего не запускать. Комп не должен уходить в Sleep (idle-сон
проверить: `powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE` — таймеры
AC/DC должны быть `0`).

## Мониторинг

**В Telegram** (только `DEVELOPER_TG_IDS`):
- `/status` — жив ли бот, масштаб, зависший добор.
- `/devstats` — по школам: активность, % отклика/явки за 4 недели.
- `/start` (dev-режим) → 📊 Dashboard — то же плюс outbox/тик/live-нагрузка (Phase 10).
- `/devmode` — переключить dev-режим ↔ режим препода (дожфудинг реального UX).

**В консоли:**
```powershell
Get-ScheduledTaskInfo -TaskName "DancePulseBot"
Get-ScheduledTaskInfo -TaskName "DancePulseWatchdog"   # LastRunTime тикает каждые 5 мин
python -m scripts.dev_stats            # то же что /devstats, с сортировкой
python -m scripts.check_pilot          # префлайт: битые данные, TZ, группы без куратора
```

- `logs/dance.log` — смотреть на `ERROR`/`Traceback`.
- `logs/heartbeat` — UTC-таймштамп, тикает раз в минуту, пока жив event loop.

**Автовосстановление (ничего руками делать не нужно):**
1. Процесс упал → Task Scheduler рестартует сам.
2. Event loop завис без падения → heartbeat стухает >10 мин → `DancePulseWatchdog`
   рестартует задачу и шлёт алерт в Telegram напрямую.
3. Занятие пропало во время простоя → `backfill_missed_surveys` при следующем
   тике сам заводит для него закрытый опрос задним числом.

## Перенос на другую машину

Капканы: SQLite в WAL-режиме (сырое копирование `.sqlite3` может потерять
последние транзакции) и двойной polling одним токеном (`TelegramConflictError`,
если старая машина не выключена, когда стартует новая). Поэтому — только через
штатные `backup_db.py`/`restore_db.py`, и **сначала выключить старую, потом
включить новую**.

### На старой машине

```powershell
python -m scripts.backup_db                      # свежий снапшот прямо сейчас
Stop-ScheduledTask -TaskName "DancePulseBot"
Disable-ScheduledTask -TaskName "DancePulseBot"   # disable, не только stop —
Disable-ScheduledTask -TaskName "DancePulseWatchdog"  # иначе watchdog сам поднимет бота обратно
python -m scripts.backup_db                       # финальный снапшот ПОСЛЕ остановки
```

Забрать из `backups/` последнюю тройку файлов: `dance-<ts>.sqlite3`,
`env-<ts>.bak`, `dance-<ts>.fsm.json` (если есть — недописанные диалоги
пользователей).

### На новой машине

```powershell
git clone https://github.com/godxblaze/dancepulsebot.git
cd dancepulsebot
python -m venv .venv
.venv\Scripts\pip install -e .

copy path\to\env-<ts>.bak .env                              # тот же токен = тот же бот
python -m scripts.restore_db path\to\dance-<ts>.sqlite3      # чистит WAL/shm, integrity_check
copy path\to\dance-<ts>.fsm.json dance.sqlite3.fsm.json      # опционально

python -m scripts.check_pilot

powershell -ExecutionPolicy Bypass -File scripts/install_bot_task.ps1
powershell -ExecutionPolicy Bypass -File scripts/install_backup_task.ps1
powershell -ExecutionPolicy Bypass -File scripts/install_watchdog_task.ps1
Start-ScheduledTask -TaskName "DancePulseBot"
```

### Проверка и добивание хвостов

- `/status` — те же цифры, что были на старой машине.
- `logs/dance.log` — без `TelegramConflictError` (если есть — где-то ещё жив
  старый polling).
- Через сутки-двое стабильной работы — снести задачи на старой машине:
  `Unregister-ScheduledTask -TaskName "DancePulseBot","DancePulseBackup","DancePulseWatchdog" -Confirm:$false`.
