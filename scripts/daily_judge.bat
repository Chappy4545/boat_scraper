@echo off
cd /d "c:\Users\kcs15\OneDrive\デスクトップ\boat_scraper"

REM Use the real python, not the Microsoft Store alias in WindowsApps.
REM The alias works in an interactive shell but fails under Task Scheduler
REM (it tries to open the Store and returns exit code 1).
set "PY=C:\Users\kcs15\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if not exist "%PY%" set "PY=C:\Users\kcs15\AppData\Local\Python\bin\python.exe"

if not exist "logs" mkdir "logs"
set "LOG=logs\task_judge.log"
echo [%date% %time%] JUDGE start >> "%LOG%"

"%PY%" main.py collect_results >> "%LOG%" 2>&1
"%PY%" main.py judge >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] JUDGE failed >> "%LOG%"
    exit /b 1
)

REM Warn only when results fall below the statistical threshold.
"%PY%" scripts\watchdog.py >> "%LOG%" 2>&1

git add docs/data/
git diff --cached --quiet || git commit -m "auto: judge results %date%"
REM Remote may have moved (cloud workflows push too). Rebase before pushing.
REM Stash anything uncommitted first: rebase refuses to run with a dirty tree,
REM which happens whenever files are being edited outside this batch.
git stash push -u -m "auto-stash before pull" >> "%LOG%" 2>&1
git pull --rebase -X theirs origin master >> "%LOG%" 2>&1
git push >> "%LOG%" 2>&1
git stash pop >> "%LOG%" 2>&1
echo [%date% %time%] JUDGE done >> "%LOG%"
