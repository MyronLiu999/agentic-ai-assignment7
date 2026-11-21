param([ValidateSet('tune', 'final')][string]$Phase = 'tune')

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:PYTHONUTF8 = '1'
$python = Join-Path (Get-Location) '.venv\Scripts\python.exe'
if (Test-Path '.env') {
    Get-Content '.env' | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
            [Environment]::SetEnvironmentVariable(
                $matches[1], $matches[2].Trim('"').Trim("'"), 'Process')
        }
    }
}
function Invoke-Judge {
    param([string[]]$JudgeArgs)
    & $python -X utf8 -m eval.judge @JudgeArgs
    if ($LASTEXITCODE -ne 0) { throw 'Judge stopped with an error; completed verdicts are retained.' }
}
& $python -X utf8 -m unittest tests.test_eval
if ($LASTEXITCODE -ne 0) { throw 'Evaluation tests failed.' }
if ($Phase -eq 'tune') {
    Invoke-Judge @('tune')
    Write-Host 'Tuning complete. Review these results before running the held-out test set. No commit or push performed.'
} else {
    & $python -X utf8 'reports/check_frozen_judge_07.py'
    if ($LASTEXITCODE -ne 0) { throw 'Frozen judge verification failed.' }
    foreach ($run in 1..3) { Invoke-Judge @('test', "$run") }
    Invoke-Judge @('versions')
    Invoke-Judge @('recorded')
    Invoke-Judge @('report')
    Write-Host 'Judge validation complete. No commit or push performed.'
}
