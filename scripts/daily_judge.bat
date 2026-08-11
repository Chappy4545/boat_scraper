@echo off
cd /d "c:\Users\kcs15\OneDrive\デスクトップ\boat_scraper"

REM Same reason as daily_update.bat: avoid the Store alias.
set "PY=C:\Users\kcs15\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if not exist "%PY%" set "PY=C:\Users\kcs15\AppData\Local\Python\bin\python.exe"

set "LOG=logs\task_judge.log"
echo [%date% %time%] JUDGE start >> "%LOG%"

"%PY%" main.py collect_results >> "%LOG%" 2>&1
"%PY%" main.py judge >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] JUDGE failed >> "%LOG%"
    exit /b 1
)

git add docs/data/
git diff --cached --quiet || git commit -m "auto: judge results %date%"
git push >> "%LOG%" 2>&1
echo [%date% %time%] JUDGE done >> "%LOG%"
