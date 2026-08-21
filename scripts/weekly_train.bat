@echo off
REM Weekly retrain. Was a bare "python.exe main.py train" task whose
REM WorkingDirectory had been stored mojibake --
REM   c:\Users\kcs15\OneDrive\<garbled>\boat_scraper
REM so every run died with 0x8007010B (the directory name is invalid) and
REM the model had not been retrained since 2026-08-12. Nothing logged it.
REM Going through a .bat removes the whole problem: %~dp0 resolves at run
REM time, so no path is ever stored anywhere.
cd /d "%~dp0.."

set "PY=C:\Users\kcs15\AppData\Local\Python\pythoncore-3.14-64\python.exe"
if not exist "%PY%" set "PY=C:\Users\kcs15\AppData\Local\Python\bin\python.exe"

REM Training reads the whole 385MB database and takes about 8 minutes.
REM Battery sleep on this machine is 3 minutes and it only offers S0 Low
REM Power Idle, where SetThreadExecutionState does nothing, so the run
REM would be suspended part way through. Hold sleep off, restore after.
set "OLDSLEEP=180"
for /f %%v in ('powershell -NoProfile -Command "[Convert]::ToInt32((((powercfg /q SCHEME_CURRENT SUB_SLEEP STANDBYIDLE) -match 'DC')[0] -split ':')[1].Trim(),16)"') do set "OLDSLEEP=%%v"
if "%OLDSLEEP%"=="2700" set "OLDSLEEP=180"
powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 2700 >nul 2>&1
powercfg /setactive SCHEME_CURRENT >nul 2>&1

if not exist "logs" mkdir "logs"
set "LOG=logs\task_train.log"
echo [%date% %time%] TRAIN start >> "%LOG%"

REM Don't fight the collect/judge for the database. A missed weekly trigger
REM fires when the machine next wakes, which is the same moment the morning
REM update starts.
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\wait_for_judge.ps1" -MaxWaitMin 90 -IncludeUpdate >> "%LOG%" 2>&1
if errorlevel 1 echo [%date% %time%] still busy after 90 min - going ahead >> "%LOG%"

git fetch origin master >> "%LOG%" 2>&1
git -c rebase.autoStash=true rebase -X theirs origin/master >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] pre-run rebase failed - aborting, using local copy >> "%LOG%"
    git rebase --abort >> "%LOG%" 2>&1
)

"%PY%" main.py train >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE %OLDSLEEP% >nul 2>&1
powercfg /setactive SCHEME_CURRENT >nul 2>&1
if not "%RC%"=="0" (
    echo [%date% %time%] TRAIN failed rc=%RC% >> "%LOG%"
    exit /b 1
)

REM The model files are tracked in git and the cloud predicts with them.
REM Leaving them unstaged makes the next run's rebase fail and the new model
REM never reaches the cloud.
git add data/processed/models >> "%LOG%" 2>&1
git diff --cached --quiet || git commit -m "auto: retrain %date%" >> "%LOG%" 2>&1
git fetch origin master >> "%LOG%" 2>&1
git -c rebase.autoStash=true rebase -X theirs origin/master >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [%date% %time%] rebase failed - aborting, keeping local >> "%LOG%"
    git rebase --abort >> "%LOG%" 2>&1
) else (
    git push >> "%LOG%" 2>&1
)
echo [%date% %time%] TRAIN done >> "%LOG%"
