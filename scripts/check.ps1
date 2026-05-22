# Dance Pulse — gate check: ruff + pytest.
# Used by the Stop hook in .claude/settings.json. Exit 2 blocks completion.
$ErrorActionPreference = "Continue"
Set-Location (Split-Path -Parent $PSScriptRoot)

$ruffOut = & python -m ruff check app scripts tests 2>&1
$ruffCode = $LASTEXITCODE
$pytestOut = & python -m pytest -q 2>&1
$pytestCode = $LASTEXITCODE

if ($ruffCode -ne 0 -or $pytestCode -ne 0) {
    $err = [Console]::Error
    $err.WriteLine("Dance Pulse: проверка не прошла (ruff=$ruffCode, pytest=$pytestCode) — почини перед завершением.")
    $err.WriteLine("--- ruff ---")
    $err.WriteLine(($ruffOut | Out-String))
    $err.WriteLine("--- pytest ---")
    $err.WriteLine(($pytestOut | Out-String))
    exit 2
}
Write-Output "Dance Pulse: ruff + pytest OK"
exit 0
