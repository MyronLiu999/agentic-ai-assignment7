param([int]$Jobs = 2)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:PYTHONUTF8 = '1'
$env:REVIEWER = 'scripted:approve-all'
$env:REPOMIND_STORE = 'empty'
$python = Join-Path (Get-Location) '.venv\Scripts\python.exe'

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Program exited with code $LASTEXITCODE. Fix the error before continuing."
    }
}

if ($Jobs -lt 1) { throw 'Jobs must be at least 1.' }
if (Test-Path '.env') {
    Get-Content '.env' | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
            [Environment]::SetEnvironmentVariable(
                $matches[1], $matches[2].Trim('"').Trim("'"), 'Process')
        }
    }
}
# Fix experimental controls after loading the key file.
$env:REVIEWER = 'scripted:approve-all'
$env:REPOMIND_STORE = 'empty'
# Reject ambient experimental switches rather than silently running a different arm.
Invoke-Checked $python @('-X', 'utf8', '-c',
    'from harness import switches; assert switches.current() == switches.DEFAULTS, "Unset experimental switches before the pilot"')

Write-Host 'Checking that you have committed and pushed the experiment inputs...'
Invoke-Checked 'git' @('ls-files', '--error-unmatch', 'eval/subsets/claim.txt', 'eval/golden/v1/manifest.yaml')
Invoke-Checked 'git' @('diff', '--exit-code', 'HEAD', '--', 'eval', 'settings.yaml', 'pricing.yaml', 'tests/test_eval.py')
$branch = (& git branch --show-current).Trim()
if ($LASTEXITCODE -ne 0 -or -not $branch) { throw 'Use a named branch, not detached HEAD.' }
Invoke-Checked 'git' @('fetch', 'origin')
Invoke-Checked 'git' @('merge-base', '--is-ancestor', 'HEAD', "origin/$branch")

Write-Host 'Testing evaluation code and API connection...'
Invoke-Checked $python @('-X', 'utf8', '-m', 'unittest', 'tests.test_eval')
Invoke-Checked $python @('-X', 'utf8', '-m', 'harness.ping')

foreach ($way in @('planned', 'agent')) {
    Write-Host "Running $way : 8 tasks x 3 sweeps. Completed runs are skipped on restart."
    Invoke-Checked $python @('-X', 'utf8', 'run.py', '--tasks', 'eval/golden/v1/tasks',
        '--subset', 'eval/subsets/claim.txt', '--way', $way, '--runs', '3', '--jobs', "$Jobs")
}
Invoke-Checked $python @('-X', 'utf8', '-m', 'eval.results_07', '--arm', 'A', '--way', 'planned', '--claim', 'plan')
Invoke-Checked $python @('-X', 'utf8', '-m', 'eval.results_07', '--arm', 'B', '--way', 'agent', '--claim', 'plan')
Invoke-Checked $python @('-X', 'utf8', 'reports/pilot_summary_07.py')
Write-Host 'Pilot complete. No commit or push was performed. Stop here before sweep 4.'
