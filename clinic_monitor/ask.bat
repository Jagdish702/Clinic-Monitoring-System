@echo off
rem Ask what is happening at a clinic. Works from cmd.exe, PowerShell, or a
rem double-click - no shell-specific syntax needed.
rem
rem   ask.bat "CUREBAY BANAMALIPUR"
rem   ask.bat "CUREBAY BANAMALIPUR" 120
setlocal

set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo.
    echo Could not find the Python environment at:
    echo   %PY%
    echo Rebuild it with:  python -m venv .venv ^&^& .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

set "CLINIC=%~1"
if "%CLINIC%"=="" (
    echo.
    echo Usage:  ask.bat "CLINIC NAME" [seconds]
    echo Example: ask.bat "CUREBAY BANAMALIPUR" 60
    echo.
    echo Looking up the clinics in your Hik-Connect app...
    pushd "%~dp0"
    "%PY%" ask.py --list-clinics
    popd
    echo.
    pause
    exit /b 1
)

set "SECS=%~2"
if "%SECS%"=="" set "SECS=60"

pushd "%~dp0"
"%PY%" ask.py --open "%CLINIC%" --duration %SECS% --question "what is happening there right now?"
set "RC=%ERRORLEVEL%"
popd

echo.
if not "%RC%"=="0" echo Finished with errors (exit code %RC%).
pause
exit /b %RC%
