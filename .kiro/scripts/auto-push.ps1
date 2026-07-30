# ---------------------------------------------------------------------------
# 변경된 파일을 자동으로 커밋해서 GitHub(kingkong-inventory)에 올린다.
#   대상: https://github.com/qorckgus1124/kingkong-inventory  (remote 이름: inventory)
#   호출: .kiro/hooks/*.json (파일 저장 시 / 에이전트 작업 완료 시 자동 실행)
#   수동 실행도 가능: powershell -File .kiro\scripts\auto-push.ps1 -Reason "수동"
# ---------------------------------------------------------------------------
param(
  [string]$Reason = "auto"
)

$ErrorActionPreference = "Continue"
$env:GIT_TERMINAL_PROMPT = 0   # 자격증명 입력창이 떠서 멈추는 것을 방지 (저장된 자격증명만 사용)

# 저장소 루트로 이동 (.kiro/scripts 기준 두 단계 위)
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $repoRoot

$logPath = Join-Path $repoRoot ".kiro\auto-push.log"

function Write-Log($text) {
  $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $text
  Add-Content -Path $logPath -Value $line -Encoding UTF8
}

# 동시 실행 방지 (파일을 여러 개 연속 저장할 때 커밋이 겹치지 않도록)
$lockPath = Join-Path $repoRoot ".kiro\auto-push.lock"
if (Test-Path $lockPath) {
  $age = (Get-Date) - (Get-Item $lockPath).LastWriteTime
  if ($age.TotalMinutes -lt 5) {
    Write-Log "이미 실행 중이라 건너뜀 ($Reason)"
    exit 0
  }
}
New-Item -ItemType File -Path $lockPath -Force | Out-Null

try {
  # 변경 사항이 없으면 아무것도 하지 않는다
  $changes = git status --porcelain
  if (-not $changes) {
    Write-Log "변경 없음 - 건너뜀 ($Reason)"
    exit 0
  }

  $fileCount = ($changes | Measure-Object).Count
  git add -A 2>&1 | Out-Null

  # 스테이징 후에도 실제 변경이 없으면(무시 대상만 바뀐 경우) 종료
  git diff --cached --quiet
  if ($LASTEXITCODE -eq 0) {
    Write-Log "커밋할 변경 없음 - 건너뜀 ($Reason)"
    exit 0
  }

  $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  $msg = "auto: $Reason - $fileCount개 파일 변경 ($stamp)"
  $commitOut = git commit -m $msg 2>&1
  Write-Log ("커밋: " + $msg)

  # 원격이 앞서 있으면(웹에서 직접 수정한 경우 등) 먼저 합친 뒤 올린다
  $pullOut = git pull --rebase --autostash inventory main 2>&1
  if ($LASTEXITCODE -ne 0) {
    Write-Log ("실패(pull): " + ($pullOut -join " | "))
    Write-Log "충돌이 있어 자동 업로드를 중단했습니다. 수동으로 해결해주세요."
    exit 1
  }

  $pushOut = git push inventory main 2>&1
  if ($LASTEXITCODE -ne 0) {
    Write-Log ("실패(push): " + ($pushOut -join " | "))
    exit 1
  }

  Write-Log "업로드 완료 -> kingkong-inventory/main"
  exit 0
}
finally {
  Remove-Item $lockPath -Force -ErrorAction SilentlyContinue
}
