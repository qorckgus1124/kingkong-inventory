@echo off
chcp 65001 > nul
echo ============================================================
echo  재고 관리 프로그램을 준비하고 있습니다. 잠시만 기다려주세요...
echo ============================================================

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [오류] Python이 설치되어 있지 않습니다.
    echo https://www.python.org/downloads/ 에서 Python을 먼저 설치해주세요.
    echo 설치 시 "Add Python to PATH"에 반드시 체크해주세요.
    pause
    exit /b
)

pip show flask >nul 2>nul
if %errorlevel% neq 0 (
    echo Flask 패키지를 설치합니다. ^(최초 1회만^)
    pip install -r requirements.txt
)

start "" http://127.0.0.1:5000
python app.py

pause