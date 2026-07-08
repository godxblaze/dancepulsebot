# Watchdog для DancePulseBot: ловит зависание event loop'а (heartbeat умер),
# которое Task Scheduler сам не видит — процесс остаётся «зелёным».
# Рестартует задачу и алертит разработчика напрямую через Telegram Bot API
# (bot.py тут не вызывается — он и есть то, что могло зависнуть).
# Регистрируется через scripts/install_watchdog_task.ps1 (каждые 5 минут).
param(
    [string]$TaskName = "DancePulseBot",
    [int]$StaleMinutes = 10
)
$ErrorActionPreference = "Stop"

$proj = (Resolve-Path "$PSScriptRoot\..").Path
$envPath = Join-Path $proj ".env"
$heartbeatPath = Join-Path $proj "logs\heartbeat"
$statePath = Join-Path $proj "logs\watchdog-last-alert.txt"

function Get-EnvValue {
    param([string]$Key)
    if (-not (Test-Path $envPath)) { return $null }
    $line = Get-Content -Path $envPath | Where-Object { $_ -match "^\s*$Key\s*=" } | Select-Object -First 1
    if (-not $line) { return $null }
    return ($line -split "=", 2)[1].Trim().Trim('"').Trim("'")
}

function Send-DeveloperAlert {
    param([string]$Text)
    $token = Get-EnvValue -Key "BOT_TOKEN"
    $devId = Get-EnvValue -Key "DEVELOPER_ID"
    if (-not $token -or -not $devId) { return }
    # Анти-спам: не чаще 1 алерта в час — иначе застрявший бот заваливает
    # разработчика сообщением каждые 5 минут до ручного вмешательства.
    if (Test-Path $statePath) {
        $last = Get-Content -Path $statePath -Raw -ErrorAction SilentlyContinue
        $lastTime = $null
        try { $lastTime = [DateTimeOffset]::Parse($last.Trim()) } catch { $lastTime = $null }
        if ($lastTime -and ((Get-Date).ToUniversalTime() - $lastTime.UtcDateTime) -lt (New-TimeSpan -Hours 1)) {
            return
        }
    }
    $uri = "https://api.telegram.org/bot$token/sendMessage"
    try {
        Invoke-RestMethod -Uri $uri -Method Post -Body @{ chat_id = $devId; text = $Text } | Out-Null
    } catch {
        # Best-effort — если сам Telegram недоступен, рестарт задачи всё равно уже произошёл.
    }
    (Get-Date).ToUniversalTime().ToString("o") | Set-Content -Path $statePath
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$info = if ($task) { Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue } else { $null }

$needsRestart = $false
$reason = $null

if (-not $task -or $task.State -ne "Running") {
    $needsRestart = $true
    $reason = if (-not $task) { "задача '$TaskName' не найдена" } else { "задача в состоянии $($task.State)" }
} elseif (-not (Test-Path $heartbeatPath)) {
    # Первый запуск после старта задачи — heartbeat ещё не успел записаться
    # (первый job идёт сразу, но даём 15 минут запаса), это не повод рестартовать.
    if ($info -and $info.LastRunTime -and ((Get-Date) - $info.LastRunTime) -gt (New-TimeSpan -Minutes 15)) {
        $needsRestart = $true
        $reason = "heartbeat-файл отсутствует спустя 15+ мин после старта задачи"
    }
} else {
    $raw = Get-Content -Path $heartbeatPath -Raw -ErrorAction SilentlyContinue
    $hbTime = $null
    if ($raw) {
        try { $hbTime = [DateTimeOffset]::Parse($raw.Trim()) } catch { $hbTime = $null }
    }
    if (-not $hbTime) {
        $needsRestart = $true
        $reason = "heartbeat-файл повреждён или пуст"
    } else {
        $age = (Get-Date).ToUniversalTime() - $hbTime.UtcDateTime
        if ($age -gt (New-TimeSpan -Minutes $StaleMinutes)) {
            $needsRestart = $true
            $reason = "heartbeat устарел на $([int]$age.TotalMinutes) мин (event loop завис?)"
        }
    }
}

if ($needsRestart) {
    if ($task) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Send-DeveloperAlert -Text "⚠️ Watchdog перезапустил '$TaskName': $reason"
    } else {
        Send-DeveloperAlert -Text "⚠️ Watchdog не смог перезапустить '$TaskName': $reason. Нужна ручная регистрация задачи (install_bot_task.ps1)."
    }
}
