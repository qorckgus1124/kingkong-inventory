# ---------------------------------------------------------------------------
# Auto commit + push to GitHub (kingkong-inventory).
#   target repo : https://github.com/qorckgus1124/kingkong-inventory
#   git remote  : inventory
#   called by   : .kiro/hooks/auto-push-on-save.json  (PostFileSave)
#                 .kiro/hooks/auto-push-on-stop.json  (Stop)
#   manual run  : powershell -NoProfile -File .kiro\scripts\auto-push.ps1 -Reason manual
#
# NOTE: This file must stay ASCII-only.
#   Windows PowerShell 5.1 reads .ps1 files as the system ANSI code page (cp949 on
#   Korean Windows) when there is no UTF-8 BOM, which corrupts non-ASCII text and
#   breaks parsing. Korean strings used in commit messages live in
#   auto-push-messages.json and are read as UTF-8 at runtime instead.
# ---------------------------------------------------------------------------
param(
  [ValidateSet("save", "done", "manual", "auto")]
  [string]$Reason = "manual"
)

$ErrorActionPreference = "Continue"
$env:GIT_TERMINAL_PROMPT = 0   # never open a credential prompt (use stored credentials only)

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $repoRoot

$logPath = Join-Path $repoRoot ".kiro\auto-push.log"
$lockPath = Join-Path $repoRoot ".kiro\auto-push.lock"
$msgJsonPath = Join-Path $PSScriptRoot "auto-push-messages.json"

function Write-Log([string]$text) {
  $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $text
  [System.IO.File]::AppendAllText($logPath, $line + "`r`n", (New-Object System.Text.UTF8Encoding($false)))
}

# Korean labels are loaded from a UTF-8 json file so this script stays ASCII-only.
$labels = @{ save = "save"; done = "done"; manual = "manual"; auto = "auto"; changed_suffix = " files changed" }
if (Test-Path $msgJsonPath) {
  try {
    $raw = [System.IO.File]::ReadAllText($msgJsonPath, [System.Text.Encoding]::UTF8)
    $parsed = $raw | ConvertFrom-Json
    foreach ($k in @("save", "done", "manual", "auto", "changed_suffix")) {
      if ($parsed.$k) { $labels[$k] = [string]$parsed.$k }
    }
  } catch {
    Write-Log "warn: could not read auto-push-messages.json, using ASCII labels"
  }
}
$reasonText = $labels[$Reason]

# Skip if another run is in progress (several files saved in a row).
if (Test-Path $lockPath) {
  $age = (Get-Date) - (Get-Item $lockPath).LastWriteTime
  if ($age.TotalMinutes -lt 5) {
    Write-Log "skip: already running ($Reason)"
    exit 0
  }
}
New-Item -ItemType File -Path $lockPath -Force | Out-Null

try {
  $changes = git status --porcelain
  if (-not $changes) {
    Write-Log "skip: nothing to commit ($Reason)"
    exit 0
  }

  $fileCount = ($changes | Measure-Object).Count
  git add -A 2>&1 | Out-Null

  git diff --cached --quiet
  if ($LASTEXITCODE -eq 0) {
    Write-Log "skip: no staged change ($Reason)"
    exit 0
  }

  $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  $msg = "auto: " + $reasonText + " - " + $fileCount + $labels["changed_suffix"] + " (" + $stamp + ")"

  # Passing the message with -m would be mangled by the console code page,
  # so write it as UTF-8 (no BOM) and hand it to git with -F.
  $msgFile = Join-Path $env:TEMP "kkv-commit-msg.txt"
  [System.IO.File]::WriteAllText($msgFile, $msg, (New-Object System.Text.UTF8Encoding($false)))
  git -c i18n.commitEncoding=UTF-8 commit -F $msgFile 2>&1 | Out-Null
  $commitCode = $LASTEXITCODE
  Remove-Item $msgFile -Force -ErrorAction SilentlyContinue
  if ($commitCode -ne 0) {
    Write-Log "failed: git commit exit=$commitCode"
    exit 1
  }
  Write-Log ("commit: " + $msg)

  # Bring in remote changes first (e.g. edits made directly on github.com).
  $pullOut = git pull --rebase --autostash inventory main 2>&1
  if ($LASTEXITCODE -ne 0) {
    Write-Log ("failed(pull): " + ($pullOut -join " | "))
    exit 1
  }

  $pushOut = git push inventory main 2>&1
  if ($LASTEXITCODE -ne 0) {
    Write-Log ("failed(push): " + ($pushOut -join " | "))
    exit 1
  }

  Write-Log "pushed -> kingkong-inventory/main"
  exit 0
}
finally {
  Remove-Item $lockPath -Force -ErrorAction SilentlyContinue
}
