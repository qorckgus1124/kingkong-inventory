# 중단해 둔 훅 (GitHub 자동 업로드)

자동 업로드를 잠시 멈추기 위해 훅 파일을 이 폴더로 옮겨 두었습니다.
Kiro는 `.kiro/hooks/*.json` 만 읽으므로, 이 폴더에 있는 동안에는 동작하지 않습니다.

## 다시 켜는 방법

두 파일을 `.kiro/hooks/` 폴더로 옮기고 세션을 새로 시작하면 됩니다.

- `auto-push-on-save.json` : 파일을 저장할 때마다 업로드
- `auto-push-on-stop.json` : Kiro 작업이 끝날 때 업로드

## 수동으로 한 번만 올리고 싶을 때

훅이 꺼져 있어도 아래 명령으로 언제든 직접 올릴 수 있습니다.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .kiro\scripts\auto-push.ps1 -Reason manual
```

업로드 결과는 `.kiro/auto-push.log` 에 기록됩니다.
