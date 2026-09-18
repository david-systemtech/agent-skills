# Put this repo's skills on a Windows machine.
#
#   .\install.ps1              clone (or fast-forward) and link ~\.agents\skills
#   .\install.ps1 -Backup      same, moving an existing ~\.agents\skills aside first
#   .\install.ps1 -Schedule    also pull hourly with a scheduled task
#
# ~\.agents\skills is the one directory both Claude (through Artemis) and
# Codex read, so linking it is the whole install. The link is a directory
# junction, which needs no administrator rights. Safe to run again.
#
# AGENT_SKILLS_REPO overrides the clone URL, AGENT_SKILLS_HOME the ~\.agents root.
param(
  [switch]$Backup,
  [switch]$Schedule
)
$ErrorActionPreference = 'Stop'

$RepoUrl = if ($env:AGENT_SKILLS_REPO) { $env:AGENT_SKILLS_REPO } else { 'https://github.com/david-systemtech/agent-skills.git' }
$Root = if ($env:AGENT_SKILLS_HOME) { $env:AGENT_SKILLS_HOME } else { Join-Path $HOME '.agents' }
$Clone = Join-Path $Root 'skills-repo'
$Link = Join-Path $Root 'skills'
$Target = Join-Path $Clone 'skills'
$TaskName = 'Agent Skills Pull'

$git = (Get-Command git -ErrorAction SilentlyContinue).Source
if (-not $git) { throw 'git is not installed' }
New-Item -ItemType Directory -Force -Path $Root | Out-Null

if (Test-Path (Join-Path $Clone '.git')) {
  & $git -C $Clone pull --ff-only --quiet
  if ($LASTEXITCODE) { throw "git pull failed in $Clone" }
  Write-Host "updated $Clone"
} else {
  & $git clone --quiet $RepoUrl $Clone
  if ($LASTEXITCODE) { throw "git clone failed for $RepoUrl" }
  Write-Host "cloned into $Clone"
}

$existing = Get-Item -LiteralPath $Link -Force -ErrorAction SilentlyContinue
if ($existing) {
  if ($existing.LinkType) {
    $current = @($existing.Target)[0]
    if ($current.TrimEnd('\') -ne $Target.TrimEnd('\')) {
      throw "$Link already links to $current - remove that link and run again"
    }
  } elseif (-not (Get-ChildItem -LiteralPath $Link -Force)) {
    Remove-Item -LiteralPath $Link
    $existing = $null
  } elseif ($Backup) {
    $aside = Join-Path $Root ("skills.backup-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Move-Item -LiteralPath $Link -Destination $aside
    Write-Host "moved the existing $Link to $aside"
    $existing = $null
  } else {
    $names = (Get-ChildItem -LiteralPath $Link -Force | ForEach-Object { "  $($_.Name)" }) -join "`n"
    throw "$Link already holds:`n$names`nrun again with -Backup to move it aside (nothing is deleted)"
  }
}
if (-not $existing) {
  New-Item -ItemType Junction -Path $Link -Target $Target | Out-Null
}
Write-Host "$Link -> $Target"

if ($Schedule) {
  # conhost --headless, because on Windows 11 a task that starts a console
  # program otherwise opens a Windows Terminal window every time it runs.
  $action = New-ScheduledTaskAction -Execute 'conhost.exe' `
    -Argument "--headless `"$git`" -C `"$Clone`" pull --ff-only --quiet"
  $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Hours 1)
  $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null
  Write-Host "scheduled task '$TaskName' pulls hourly"
}

$count = @(Get-ChildItem -LiteralPath $Link -Directory |
  Where-Object { Test-Path (Join-Path $_.FullName 'SKILL.md') }).Count
Write-Host "$count skill(s) installed"
