<#
.SYNOPSIS
  Inject every fault type (several rounds) for the Week 8 end-to-end accuracy evaluation.

.DESCRIPTION
  scripts/inject_fault.py refuses to run on main and needs a completely clean tree, so this works in a throwaway
  git worktree next to the repository and removes it afterwards. For each round it injects:
    tc01_unit_test, tc02_compile_syntax, tc02_compile_import, tc10_flaky
  Each injection pushes a branch `fault/<scenario>-<timestamp>` to GitHub (with your git credentials); Jenkins finds it
  on its next scan (every 2 minutes), builds it, and the webhook opens an incident.

  The fault branches are NOT deleted here: Jenkins must still be able to build them. Their names go to
  results/fault_branches.txt; delete them with `inject_fault.py revert <branch>` once the evaluation is done.

  tc10_flaky only fails some of the time. inject_fault.py triggers 8 builds for it through the Jenkins API. It reads
  JENKINS_USER, JENKINS_API_TOKEN and JENKINS_URL from the shell environment, not from .env, so this script borrows
  JENKINS_USER and JENKINS_API_TOKEN from .env when the shell has none, and finds the Jenkins port from Docker (your
  .env JENKINS_URL is the address the containers use, which can differ from the one on this machine). Without
  credentials it builds once and may pass, which simply gives no incident.

.EXAMPLE
  powershell -File scripts\inject_all_faults.ps1 -Rounds 2
  .venv\Scripts\python scripts\evaluate_end_to_end.py          # after the builds and reports have finished
#>
param(
    [int]$Rounds = 2,
    [int]$PauseSeconds = 20
)

# Native commands write progress to stderr; do not turn that into terminating errors on Windows PowerShell 5.1.
$ErrorActionPreference = "Continue"

$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $repo ".venv\Scripts\python.exe"
$work = Join-Path (Split-Path $repo -Parent) "jenkins_guadrian_fault"
# A new name every run: a leftover branch (or worktree) from an earlier attempt must not stop this one.
$throwaway = "demo/fault-run-" + (Get-Date -Format "yyyyMMddHHmmss")
$scenarios = "tc01_unit_test", "tc02_compile_syntax", "tc02_compile_import", "tc10_flaky"

if (-not (Test-Path $python)) { Write-Error "Cannot find $python"; exit 2 }
if (Test-Path $work) { Write-Error "$work already exists: remove it first (git worktree remove --force `"$work`")"; exit 2 }
function Read-DotEnv([string]$Name) {
    $file = Join-Path $repo ".env"
    if (-not (Test-Path $file)) { return $null }
    foreach ($line in Get-Content $file) {
        if ($line -match "^\s*$Name\s*=\s*(.*?)\s*$") { return $Matches[1].Trim('"').Trim("'") }
    }
    return $null
}

if (-not $env:JENKINS_USER) { $env:JENKINS_USER = Read-DotEnv "JENKINS_USER" }
if (-not $env:JENKINS_API_TOKEN) { $env:JENKINS_API_TOKEN = Read-DotEnv "JENKINS_API_TOKEN" }
if (-not $env:JENKINS_URL) {
    # The port Docker actually published for Jenkins (8082 with a local override, 8080 by default).
    Push-Location $repo
    $mapping = docker compose port jenkins 8080 2>$null
    Pop-Location
    if ($mapping -match ":(\d+)\s*$") { $env:JENKINS_URL = "http://localhost:$($Matches[1])" } else { $env:JENKINS_URL = "http://localhost:8080" }
}
if ($env:JENKINS_USER -and $env:JENKINS_API_TOKEN) {
    Write-Host "Jenkins API: $($env:JENKINS_URL) (credentials found; tc10_flaky will trigger several builds)"
} else {
    Write-Warning "No JENKINS_USER / JENKINS_API_TOKEN in the shell or in .env: tc10_flaky will build only once and may pass (no incident)."
}

$branches = New-Object System.Collections.Generic.List[string]
git -C $repo worktree add -b $throwaway $work main | Out-Host
if ($LASTEXITCODE -ne 0) { Write-Error "could not create the worktree"; exit 2 }

Push-Location $work
try {
    for ($round = 1; $round -le $Rounds; $round++) {
        foreach ($scenario in $scenarios) {
            Write-Host ""
            Write-Host "=== round $round of ${Rounds}: $scenario ===" -ForegroundColor Cyan
            $lines = & $python "scripts\inject_fault.py" $scenario 2>&1 | ForEach-Object { "$_" }
            $lines | Out-Host
            $pushed = $lines | Select-String -Pattern "Pushed branch: (\S+)" | Select-Object -First 1
            if ($LASTEXITCODE -eq 0 -and $pushed) {
                $branches.Add($pushed.Matches[0].Groups[1].Value)
            } else {
                Write-Warning "injection failed for $scenario in round $round (see above); continuing"
            }
            Start-Sleep -Seconds $PauseSeconds   # distinct timestamps, and a gentler load on Jenkins and the LLM quota
        }
    }
}
finally {
    Pop-Location
    git -C $repo worktree remove --force $work | Out-Host
    git -C $repo branch -D $throwaway | Out-Host
}

$results = Join-Path $repo "results"
New-Item -ItemType Directory -Force -Path $results | Out-Null
$branches | Set-Content -Path (Join-Path $results "fault_branches.txt") -Encoding utf8
Write-Host ""
Write-Host "Injected $($branches.Count) fault branch(es); names saved to results\fault_branches.txt" -ForegroundColor Green
Write-Host "Jenkins scans every 2 minutes, then each build takes a few minutes. Wait ~10-15 minutes, then run:"
Write-Host "  .venv\Scripts\python scripts\evaluate_end_to_end.py"
