# Регистрирует автозапуск бота Dance Pulse при старте системы + авто-рестарт
# при падении через Windows Task Scheduler. Аналог systemd Restart=always.
# Запуск из корня проекта в PowerShell от администратора:
#   powershell -ExecutionPolicy Bypass -File scripts/install_bot_task.ps1
# Опционально: -TaskName "DancePulseBot"
param(
    [string]$TaskName = "DancePulseBot"
)
$ErrorActionPreference = "Stop"

$proj = (Resolve-Path "$PSScriptRoot\..").Path
$py = Join-Path $proj ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$action  = New-ScheduledTaskAction -Execute $py -Argument "-m app.main" -WorkingDirectory $proj
$trigger = New-ScheduledTaskTrigger -AtStartup
# RestartCount/Interval — перезапуск при падении; ExecutionTimeLimit 0 — без
# лимита по времени (бот работает постоянно). StartWhenAvailable — поднимет,
# если момент старта пропущен.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -RunLevel Highest -Force `
    -Description "Dance Pulse bot — autostart + auto-restart" | Out-Null

Write-Host "OK: задача '$TaskName' зарегистрирована — автозапуск при старте системы, рестарт каждую 1 мин при падении."
Write-Host "Запустить сейчас: Start-ScheduledTask -TaskName '$TaskName' ; логи бота — в logs/dance.log"
