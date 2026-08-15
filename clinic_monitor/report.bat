@echo off
rem Daily operational report per clinic, built from patrol observations.
rem
rem   report.bat                    today, every clinic that was patrolled
rem   report.bat 2026-08-11         a specific date
rem   report.bat 2026-08-11 "CUREBAY BANAMALIPUR"
rem   report.bat --list-days        which days have data
setlocal

set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Could not find the Python environment at "%PY%".
    pause
    exit /b 1
)

pushd "%~dp0"
if "%~1"=="--list-days" (
    "%PY%" report.py --list-days
) else if "%~1"=="" (
    "%PY%" report.py
) else if "%~2"=="" (
    "%PY%" report.py --day "%~1"
) else (
    "%PY%" report.py --day "%~1" --clinic "%~2"
)
set "RC=%ERRORLEVEL%"
popd

echo.
pause
exit /b %RC%
