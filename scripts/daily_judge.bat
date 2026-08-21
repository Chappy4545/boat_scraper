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

REM Judge the racing day that has just finished, not the calendar day.
REM This runs at 22:30, but a missed trigger fires whenever the machine
REM next wakes -- on 2026-08-17 the 08-16 run started at 09:44 and
REM judged 08-17, a day whose races had barely begun, leaving 08-16
REM unjudged again. Before noon the finished day is yesterday.
for /f %%i in ('powershell -NoProfile -Command "$d=Get-Date; if($d.Hour -lt 12){$d=$d.AddDays(-1)}; Get-Date $d -Format yyyy-MM-dd"') do set "RUNDATE=%%i"

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
set "LOG=logs\task_judge.log"
echo [%date% %time%] JUDGE start >> "%LOG%"

REM Pull BEFORE judging. The cloud rewrites docs/data/bets_<date>.json every
REM 15 minutes all day; that file is the only record of what was actually on
REM the board, and judge reads it (_sync_bets_from_json) to put the day's real
REM picks into the DB. Fetching afterwards meant judge read a copy from before
REM the racing even started, imported that, exported over the cloud's version,
REM and then pushed it with `rebase -X theirs` -- discarding the whole day.
REM 2026-08-20 measured: the cloud board ended with 12 market_blend candidates
REM and 70 final picks; judge imported the stale 48-entry morning copy
REM (log: imported=48 added=0) and the 12 candidates were lost. The candidate
REM rule read 0 for a week because of this, not because it found nothing.
git fetch origin master >> "%LOG%" 2>&1
git -c rebase.autoStash=true rebase -X theirs origin/master >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] pre-run rebase failed - aborting, judging local copy >> "%LOG%"
    git rebase --abort >> "%LOG%" 2>&1
)

"%PY%" main.py collect_results %RUNDATE% >> "%LOG%" 2>&1
"%PY%" main.py judge %RUNDATE% >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE %OLDSLEEP% >nul 2>&1
powercfg /setactive SCHEME_CURRENT >nul 2>&1
if not "%RC%"=="0" (
    echo [%date% %time%] JUDGE failed >> "%LOG%"
    exit /b 1
)

REM Warn only when results fall below the statistical threshold.
"%PY%" scripts\watchdog.py >> "%LOG%" 2>&1

REM One screen a day: did everything run, and is the evidence accruing.
REM Every failure so far has been silent, so the result also goes to
REM docs/data/health.json and the app raises it when something is off.
"%PY%" scripts\daily_check.py %RUNDATE% >> "%LOG%" 2>&1

git add docs/data/
git diff --cached --quiet || git commit -m "auto: judge results %date%"
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
echo [%date% %time%] JUDGE done >> "%LOG%"
