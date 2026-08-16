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
set "LOG=logs\task_judge.log"
echo [%date% %time%] JUDGE start >> "%LOG%"

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
