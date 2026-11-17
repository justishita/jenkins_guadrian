<#
.SYNOPSIS
  Inject every fault type (several rounds) for the Week 8 end-to-end accuracy evaluation.

.DESCRIPTION
  scripts/inject_fault.py refuses to run on main and needs a completely clean tree, so this works in a throwaway
  git worktree next to the repository and removes it afterwards. For each round it injects:
    tc01_unit_test, tc02_compile_syntax, tc02_compile_import, tc10_flaky
  Each injection pushes a branch `fault/<scenario>-<timestamp>` to GitHub (with your git credentials); Jenkins finds it
  on its next scan (every 2 minutes), builds it, and the webhook opens an incident.

  Every injection starts from a clean copy of main. git stays on the new fault branch after an injection, so without
  that reset each fault would be built on top of the previous one and a branch would carry several faults, which have no
  single right answer to score against. A failed injection (a one-off network error) is retried once.

  tc10_flaky only fails some of the time, so it needs several builds. The script asks Jenkins to scan for new branches,
  waits until it has found each flaky branch, then triggers the builds (inject_fault.py trigger). That needs a Jenkins
  user and API token: they are read from JENKINS_USER / JENKINS_API_TOKEN in the shell or, failing that, from .env, and
  the Jenkins address is the port Docker published (your .env JENKINS_URL is the address the containers use, which can
  differ from the one on this machine). Without credentials the flaky branch builds once and may pass: no incident.

  The fault branches are NOT deleted here: Jenkins must still be able to build them. Their names are appended to
  results/fault_branches.txt (evaluate_end_to_end.py --branches scores exactly those); delete them with
  `inject_fault.py revert <branch>` once the evaluation is done.

.EXAMPLE
  powershell -File scripts\inject_all_faults.ps1 -Rounds 2
  .venv\Scripts\python scripts\evaluate_end_to_end.py --branches results\fault_branches.txt   # after the builds finish
#>
param(
    [int]$Rounds = 2,
    [int]$PauseSeconds = 20,
    [int]$WaitForJenkinsMinutes = 6
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
$haveCredentials = [bool]($env:JENKINS_USER -and $env:JENKINS_API_TOKEN)
if ($haveCredentials) {
    Write-Host "Jenkins API: $($env:JENKINS_URL) (credentials found; the flaky branch will get several builds)"
} else {
    Write-Warning "No JENKINS_USER / JENKINS_API_TOKEN in the shell or in .env: tc10_flaky will build only once and may pass (no incident)."
}

function Get-JenkinsHeaders {
    $pair = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("$($env:JENKINS_USER):$($env:JENKINS_API_TOKEN)"))
    return @{ Authorization = "Basic $pair" }
}

function Start-JenkinsScan {
    # "Scan Multibranch Pipeline Now": look for new branches immediately instead of at the next 2-minute tick.
    # No crumb: signing in with an API token needs none, and a crumb fetched in another session is refused (403).
    $headers = Get-JenkinsHeaders
    try {
        Invoke-WebRequest -UseBasicParsing -Method Post -Uri "$($env:JENKINS_URL)/job/target-app/build?delay=0" -Headers $headers -TimeoutSec 30 | Out-Null
        return $true
    } catch {
        Write-Warning "could not ask Jenkins to scan now ($($_.Exception.Message)); it will find the branch on its own tick"
        return $false
    }
}

function Test-JenkinsBranch([string]$Branch) {
    # Branch jobs live at /job/target-app/job/<branch with the slash encoded twice>.
    $encoded = [uri]::EscapeDataString($Branch).Replace("%2F", "%252F")
    try {
        $reply = Invoke-WebRequest -UseBasicParsing -Uri "$($env:JENKINS_URL)/job/target-app/job/$encoded/api/json" -Headers (Get-JenkinsHeaders) -TimeoutSec 15
        return ($reply.StatusCode -eq 200)
    } catch { return $false }
}

function Invoke-Injection([string]$Scenario) {
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        # Back to a clean copy of main: the previous injection left git on its fault branch.
        git switch --force $throwaway 2>&1 | Out-Null
        git reset --hard -q 2>&1 | Out-Null
        git clean -fdq 2>&1 | Out-Null
        $lines = & $python "scripts\inject_fault.py" $Scenario 2>&1 | ForEach-Object { "$_" }
        $code = $LASTEXITCODE
        $lines | Out-Host
        $pushed = $lines | Select-String -Pattern "Pushed branch: (\S+)" | Select-Object -First 1
        if ($code -eq 0 -and $pushed) { return $pushed.Matches[0].Groups[1].Value }
        Write-Warning "$Scenario failed (attempt $attempt of 2)"
        Start-Sleep -Seconds 5
    }
    return $null
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
            $branch = Invoke-Injection $scenario
            if ($branch) { $branches.Add($branch) } else { Write-Warning "no branch for $scenario in round $round after two attempts; continuing" }
            Start-Sleep -Seconds $PauseSeconds   # distinct timestamps, and a gentler load on Jenkins and the LLM quota
        }
    }

    $flaky = @($branches | Where-Object { $_ -like "fault/tc10_flaky-*" })
    if ($flaky.Count -gt 0 -and $haveCredentials) {
        Write-Host ""
        Write-Host "=== flaky branches: waiting for Jenkins to find them, then triggering several builds each ===" -ForegroundColor Cyan
        [void](Start-JenkinsScan)
        $deadline = (Get-Date).AddMinutes($WaitForJenkinsMinutes)
        foreach ($branch in $flaky) {
            while (-not (Test-JenkinsBranch $branch) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 15 }
            if (Test-JenkinsBranch $branch) {
                & $python "scripts\inject_fault.py" trigger $branch 2>&1 | ForEach-Object { "$_" } | Out-Host
            } else {
                Write-Warning "Jenkins has not found $branch after $WaitForJenkinsMinutes minutes; trigger it later: .venv\Scripts\python scripts\inject_fault.py trigger $branch"
            }
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
$file = Join-Path $results "fault_branches.txt"
# One name per line, no byte-order mark. (An earlier version let PowerShell turn a one-line file into a plain string and
# glued every name onto one line.)
$all = New-Object System.Collections.Generic.List[string]
if (Test-Path $file) {
    foreach ($line in (Get-Content $file)) { if ("$line".Trim() -and -not $all.Contains("$line".Trim())) { $all.Add("$line".Trim()) } }
}
foreach ($branch in $branches) { if (-not $all.Contains($branch)) { $all.Add($branch) } }
[System.IO.File]::WriteAllLines($file, $all.ToArray(), (New-Object System.Text.UTF8Encoding($false)))
Write-Host ""
Write-Host "Injected $($branches.Count) fault branch(es) this run; the list is in results\fault_branches.txt" -ForegroundColor Green
Write-Host "Jenkins builds each branch in a few minutes. Wait ~10-15 minutes, then run:"
Write-Host "  .venv\Scripts\python scripts\evaluate_end_to_end.py --branches results\fault_branches.txt"
