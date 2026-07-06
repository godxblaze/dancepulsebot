# Регистрирует ежедневный бэкап базы Dance Pulse через Windows Task Scheduler.
# Запуск из корня проекта в PowerShell (для записи в планировщик может
# потребоваться запуск от администратора):
#   powershell -ExecutionPolicy Bypass -File scripts/install_backup_task.ps1
# Опционально: -Time "03:00" -TaskName "DancePulseBackup"
param(
    [string]$Time = "03:00",
    [string]$TaskName = "DancePulseBackup"
)
$ErrorActionPreference = "Stop"

$proj = (Resolve-Path "$PSScriptRoot\..").Path
$py = Join-Path $proj ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$action  = New-ScheduledTaskAction -Execute $py -Argument "-m scripts.backup_db" -WorkingDirectory $proj
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
# WakeToRun — если машина спит в 03:00, Windows её разбудит ради бэкапа;
# без этого флага StartWhenAvailable просто ждёт следующего пробуждения
# (может пропустить бэкап на день, если хост всю ночь проспал).
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun
# SYSTEM + ServiceAccount — та же дыра, что и в install_bot_task.ps1: без
# принципала задача требует залогиненного пользователя, и ночной бэкап после
# ребута без логина не выполнится. SYSTEM должен иметь доступ к $proj и .env.
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description "Daily Dance Pulse SQLite backup" | Out-Null

Write-Host "OK: задача '$TaskName' зарегистрирована — ежедневно в $Time запускает 'python -m scripts.backup_db'."
Write-Host "Проверить: Get-ScheduledTask -TaskName '$TaskName' ; запустить вручную: Start-ScheduledTask -TaskName '$TaskName'"
