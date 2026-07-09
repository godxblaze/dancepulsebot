# Dance Pulse — gate check: ruff + pytest + mypy (warning).
# Used by the Stop hook in .claude/settings.json. Exit 2 blocks completion.
$ErrorActionPreference = "Continue"
Set-Location (Split-Path -Parent $PSScriptRoot)

# Postgres-миграция (2026-07-09): backup/restore/backpressure-тесты шеллятся
# в pg_dump/pg_restore/psql/createdb/dropdb напрямую. Этот скрипт запускается
# Stop-hook'ом в СВОЁМ процессе — не наследует PATH, который агент экспортил
# в своей bash-сессии, поэтому без этого блока они падали с обманчивым
# WinError 2 (см. память postgres-local-dev-no-admin). Portable-инстанс —
# единственный доступный без прав администратора на этой машине.
$pgBin = Join-Path (Split-Path -Parent $PSScriptRoot) ".pgportable\pgsql\bin"
if (Test-Path $pgBin) {
    $env:PATH = "$pgBin;$env:PATH"
}

$ruffOut = & python -m ruff check app scripts tests 2>&1
$ruffCode = $LASTEXITCODE
# Полный суит — параллельно через pytest-xdist (в dev-deps). Фиксированные 8
# воркеров, не "auto": на многоядерной машине auto поднимает десятки воркеров
# и их старт съедает весь выигрыш. Без плагина — обычный серийный прогон.
& python -c "import xdist" 2>$null
if ($LASTEXITCODE -eq 0) {
    $pytestOut = & python -m pytest -q -n 8 2>&1
} else {
    $pytestOut = & python -m pytest -q 2>&1
}
$pytestCode = $LASTEXITCODE

# mypy — warning-only: показывает свежие type-ошибки в dialogs/handlers,
# но не блокирует. Многие false-positive (event union, generics aiogram-dialog)
# поэтому ruff+pytest остаются обязательными.
$mypyOut = & python -m mypy app/bot/dialogs app/bot/handlers --ignore-missing-imports --no-strict-optional 2>&1
$mypyCode = $LASTEXITCODE

if ($ruffCode -ne 0 -or $pytestCode -ne 0) {
    $err = [Console]::Error
    $err.WriteLine("Dance Pulse: проверка не прошла (ruff=$ruffCode, pytest=$pytestCode) — почини перед завершением.")
    $err.WriteLine("--- ruff ---")
    $err.WriteLine(($ruffOut | Out-String))
    $err.WriteLine("--- pytest ---")
    $err.WriteLine(($pytestOut | Out-String))
    exit 2
}
if ($mypyCode -ne 0) {
    Write-Output "Dance Pulse: ruff + pytest OK; mypy: $mypyCode (warnings, не блокирует — посмотри если важно)"
} else {
    Write-Output "Dance Pulse: ruff + pytest + mypy OK"
}
exit 0
