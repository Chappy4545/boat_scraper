@echo off
cd /d "c:\Users\kcs15\OneDrive\デスクトップ\boat_scraper"

REM Use the real python, not the Microsoft Store alias in WindowsApps.
REM The alias works in an interactive shell but fails under Task Scheduler
REM (it tries to open the Store and returns exit code 1).
REM This broke the 22:30 judge on 2026-08-11 and the 08:00 update on 08-12.
set "PY=C:\Users\kcs15\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if not exist "%PY%" set "PY=C:\Users\kcs15\AppData\Local\Python\bin\python.exe"

set "LOG=logs\task_update.log"
echo [%date% %time%] UPDATE start >> "%LOG%"

"%PY%" main.py update >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] UPDATE failed >> "%LOG%"
    exit /b 1
)

git add docs/data/
git diff --cached --quiet || git commit -m "auto: update predictions %date%"
git push >> "%LOG%" 2>&1
echo [%date% %time%] UPDATE done >> "%LOG%"
