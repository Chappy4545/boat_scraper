@echo off
REM Local daily sync. Since 2026-08-21 this no longer produces the picks --
REM the cloud does that at 09:30 JST (.github/workflows/morning_predict.yml).
REM All that is left here is keeping the 385MB history database current, which
REM does not care what time it runs. That is the point: the machine is
REM hibernating until its owner switches it on, and in the nine days before
REM this change the 08:00 task ran on schedule twice.
REM
REM Move to the repo root without writing the path.
REM The path contains Japanese characters; writing it here breaks when
REM the file encoding differs from the console codepage.
cd /d "%~dp0.."

REM Use the real python, not the Microsoft Store alias in WindowsApps.
REM The alias works in an interactive shell but fails under Task Scheduler
REM (it tries to open the Store and returns exit code 1).
set "PY=C:\Users\kcs15\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if not exist "%PY%" set "PY=C:\Users\kcs15\AppData\Local\Python\bin\python.exe"

REM Capture the date at launch and pass it explicitly.
REM The machine sleeps mid-run: on 2026-08-14 the judge started at
REM 23:45 and its python did not get going until 08:00 the next day,
REM so date.today() collected the wrong day and 08-13 was left empty.
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set "RUNDATE=%%i"

REM Battery sleep on this machine is 3 minutes, and it only offers S0
REM Low Power Idle, where SetThreadExecutionState has no effect -- the
REM run was being suspended about 7 minutes in, every day. Hold sleep
REM off for the length of the run and put the old value back after.
set "OLDSLEEP=180"
for /f %%v in ('powershell -NoProfile -Command "[Convert]::ToInt32((((powercfg /q SCHEME_CURRENT SUB_SLEEP STANDBYIDLE) -match 'DC')[0] -split ':')[1].Trim(),16)"') do set "OLDSLEEP=%%v"
REM If a previous run died before restoring, the captured value is our
REM own hold. Fall back to the machine default rather than making it stick.
if "%OLDSLEEP%"=="2700" set "OLDSLEEP=180"
powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 2700 >nul 2>&1
powercfg /setactive SCHEME_CURRENT >nul 2>&1

if not exist "logs" mkdir "logs"
set "LOG=logs\task_update.log"
echo [%date% %time%] UPDATE start >> "%LOG%"

REM A judge that missed its 22:30 trigger runs the moment the machine wakes,
REM which is the same second this task starts (2026-08-18: both at 8:00:06).
REM They share one SQLite file and both rebase/push, so let the judge finish
REM first -- it takes 5 minutes and yesterday belongs before today anyway.
REM Relative path on purpose: we already cd'd to the repo root, and handing
REM powershell.exe the absolute path fails -- it runs through the cp932 command
REM line and the Japanese directory name comes out mangled (verified today).
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\wait_for_judge.ps1" -MaxWaitMin 30 >> "%LOG%" 2>&1
if errorlevel 1 echo [%date% %time%] judge still running after 30 min - going ahead >> "%LOG%"

REM Pull BEFORE running, for the same reason the judge does: the cloud rewrites
REM docs/data/bets_<date>.json every 15 minutes and that file is the only record
REM of what was actually on the board. update's export_day writes over it, so
REM starting from a stale copy silently discards whatever the cloud recorded.
REM (2026-08-20: a whole day of market_blend candidates was lost this way.)
git fetch origin master >> "%LOG%" 2>&1
git -c rebase.autoStash=true rebase -X theirs origin/master >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] pre-run rebase failed - aborting, using local copy >> "%LOG%"
    git rebase --abort >> "%LOG%" 2>&1
)

REM Bring in the board the cloud captured when it made the picks. The cloud
REM runs on a throwaway database, so odds_raw_<date>.json.gz is the only copy
REM and odds cannot be fetched retrospectively.
"%PY%" main.py ingest_odds %RUNDATE% >> "%LOG%" 2>&1

REM Fill the history database only. The cloud (morning_predict) makes the
REM picks now, so predicting here would rebuild them from different odds and
REM overwrite them, and collecting odds here would overwrite the board the
REM picks were chosen on. Neither is wanted -- both destroy the record.
REM What is left does not care what time it runs.
"%PY%" main.py update %RUNDATE% --no-predict --no-odds >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE %OLDSLEEP% >nul 2>&1
powercfg /setactive SCHEME_CURRENT >nul 2>&1
if not "%RC%"=="0" (
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
