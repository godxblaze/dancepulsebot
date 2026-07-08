# Регистрирует watchdog Dance Pulse: каждые 5 минут проверяет heartbeat бота
# и состояние задачи "DancePulseBot", рестартует её при зависании и алертит
# разработчика. Аналог systemd Restart=always, но ловит зависание event loop'а
# (процесс жив, но не отвечает), а не только падение процесса.
# Запуск из корня проекта в PowerShell от администратора:
#   powershell -ExecutionPolicy Bypass -File scripts/install_watchdog_task.ps1
# Опционально: -TaskName "DancePulseWatchdog" -BotTaskName "DancePulseBot"
param(
    [string]$TaskName = "DancePulseWatchdog",
    [string]$BotTaskName = "DancePulseBot"
)
$ErrorActionPreference = "Stop"

$proj = (Resolve-Path "$PSScriptRoot\..").Path
$watchdogScript = Join-Path $proj "scripts\watchdog_bot.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$watchdogScript`" -TaskName `"$BotTaskName`"" `
    -WorkingDirectory $proj
# Once + RepetitionInterval/Duration=MaxValue — стандартный идиом для
# «повторять каждые N минут бесконечно» в Task Scheduler.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration ([TimeSpan]::MaxValue)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
# SYSTEM — та же причина, что и в install_bot_task.ps1: рестарт задачи бота
# (Stop/Start-ScheduledTask) и чтение .env должны работать без залогиненного
# пользователя (ночной ребут).
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description "Dance Pulse watchdog — restarts '$BotTaskName' on hang, alerts developer" | Out-Null

Write-Host "OK: задача '$TaskName' зарегистрирована — каждые 5 мин проверяет heartbeat/статус '$BotTaskName'."
Write-Host "Проверить: Get-ScheduledTaskInfo -TaskName '$TaskName' ; запустить вручную: Start-ScheduledTask -TaskName '$TaskName'"
