# 킹콩 재고·판매관리

매장별 재고·판매·입출고·분석을 관리하는 사내 웹앱입니다.

- 운영 주소: https://kingkong-inventory.onrender.com
- 구성: Flask + PostgreSQL(Neon) · Render에서 호스팅 (`main` 브랜치에 올리면 자동 배포)
- 설정: Render 환경 변수 `DATABASE_URL`(Neon 접속 문자열), `SECRET_KEY`

## 파일 구조

| 경로 | 내용 |
|---|---|
| `app.py` | 화면·API 대부분 |
| `auth.py` | 로그인, 계정, 매장 계정 권한 |
| `admin_reports.py` | 어드민 매장 리포트 |
| `templates/` | 화면(HTML) |
| `static/` | css·js·아이콘, 서비스워커(`sw.js`) |
| `render.yaml`, `build.sh`, `requirements.txt` | Render 배포 설정 |
