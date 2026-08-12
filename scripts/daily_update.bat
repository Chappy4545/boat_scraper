@echo off
REM Move to the repo root without writing the path.
REM The path contains Japanese characters; writing it here breaks when
REM the file encoding differs from the console codepage.
cd /d "%~dp0.."

REM Use the real python, not the Microsoft Store alias in WindowsApps.
REM The alias works in an interactive shell but fails under Task Scheduler
REM (it tries to open the Store and returns exit code 1).
set "PY=C:\Users\kcs15\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if not exist "%PY%" set "PY=C:\Users\kcs15\AppData\Local\Python\bin\python.exe"

if not exist "logs" mkdir "logs"
set "LOG=logs\task_update.log"
echo [%date% %time%] UPDATE start >> "%LOG%"

"%PY%" main.py update >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] UPDATE failed >> "%LOG%"
    exit /b 1
)

git add docs/data/
git diff --cached --quiet || git commit -m "auto: update predictions %date%"
REM Remote may have moved (cloud workflows push too). Rebase before pushing.
REM Never stash/pop by hand around this. A pop conflict writes <<<<<<< markers
REM into these very .bat files, cmd.exe cannot parse them, and the scheduled
REM task then dies silently (2026-08-13: the morning update never ran).
REM --autostash does the same job atomically, and on failure we abort so the
REM working tree is always left in a runnable state.
git fetch origin master >> "%LOG%" 2>&1
git -c rebase.autoStash=true rebase -X theirs origin/master >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] rebase failed - aborting, keeping local >> "%LOG%"
    git rebase --abort >> "%LOG%" 2>&1
) else (
    git push >> "%LOG%" 2>&1
)
echo [%date% %time%] UPDATE done >> "%LOG%"
