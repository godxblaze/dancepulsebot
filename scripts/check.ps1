# Dance Pulse — gate check: ruff + pytest + mypy (warning).
# Used by the Stop hook in .claude/settings.json. Exit 2 blocks completion.
$ErrorActionPreference = "Continue"
Set-Location (Split-Path -Parent $PSScriptRoot)

# Postgres-миграция (2026-07-10): portable-инстанс снесён, Postgres теперь
# в Docker (docker-compose.yml на 5432, docker-compose.test.yml на 5433, см.
# память postgres-local-dev-no-admin). Этот скрипт запускается Stop-hook'ом
# в СВОЁМ процессе — не наследует TEST_DATABASE_URL, который агент мог
# экспортить в своей bash-сессии, иначе conftest.py падает на дефолтном
# postgres@127.0.0.1:5432 без пароля (InvalidPasswordError).
if (-not $env:TEST_DATABASE_URL) {
    $env:TEST_DATABASE_URL = "postgresql+asyncpg://dancepulse_test:dancepulse_test@127.0.0.1:5433/dancepulse_test"
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
    # Урезаем вывод: полный ruff+pytest на провале раздувал контекст Claude
    # на сотни тысяч токенов за один Stop (падал каждый раз из-за не
    # выставленного TEST_DATABASE_URL, см. правку выше) — держим сводку
    # компактной, детали смотреть вручную через `python -m pytest`.
    $err = [Console]::Error
    $err.WriteLine("Dance Pulse: проверка не прошла (ruff=$ruffCode, pytest=$pytestCode) — почини перед завершением.")
    $err.WriteLine("--- ruff (последние 60 строк) ---")
    $err.WriteLine((($ruffOut | Select-Object -Last 60) | Out-String))
    $err.WriteLine("--- pytest (последние 60 строк) ---")
    $err.WriteLine((($pytestOut | Select-Object -Last 60) | Out-String))
    exit 2
}
if ($mypyCode -ne 0) {
    Write-Output "Dance Pulse: ruff + pytest OK; mypy: $mypyCode (warnings, не блокирует — посмотри если важно)"
} else {
    Write-Output "Dance Pulse: ruff + pytest + mypy OK"
}
exit 0
