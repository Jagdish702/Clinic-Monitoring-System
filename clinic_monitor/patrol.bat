@echo off
rem Continuous patrol - starts everything it needs.
rem
rem   patrol.bat              emulator + dashboard + 60s per clinic, until Ctrl+C
rem   patrol.bat 120          two minutes per clinic
rem   patrol.bat 90 3         90 seconds each, stop after 3 full rounds
rem   patrol.bat 60 0 --clinics BANAMALIPUR,NAYAHAT
rem
rem Uses the Android emulator by default. To patrol through a phone on a USB
rem cable instead, pass --phone as the third argument.
setlocal EnableDelayedExpansion

set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo.
    echo Could not find the Python environment at:
    echo   %PY%
    echo Rebuild it with:
    echo   python -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r clinic_monitor\requirements.txt
    echo.
    pause
    exit /b 1
)

rem First argument: seconds per clinic. Second: rounds (0 = forever).
set "SECS=%~1"
if "%SECS%"=="" set "SECS=60"
set "ROUNDS=%~2"
if "%ROUNDS%"=="" set "ROUNDS=0"

rem Remaining arguments pass through. --phone switches off the emulator.
set "EXTRA="
set "DEVICE=--emulator"
if not "%~3"=="" (
    shift
    shift
    :collect
    if "%~1"=="" goto done
    if /I "%~1"=="--phone" (
        set "DEVICE="
    ) else (
        set "EXTRA=!EXTRA! %1"
    )
    shift
    goto collect
)
:done

pushd "%~dp0"

rem Start the dashboard unless something is already serving on the port.
netstat -an | findstr /C:"127.0.0.1:8000" | findstr /I "LISTENING" >nul
if errorlevel 1 (
    echo Starting the dashboard...
    start "Clinic Monitor dashboard" /min "%PY%" dashboard\app.py
    rem Give Flask a moment so the first page load does not race the patrol.
    ping -n 4 127.0.0.1 >nul
) else (
    echo Dashboard already running.
)

echo.
echo ============================================================
echo  Patrolling every clinic - %SECS% seconds each
if "%ROUNDS%"=="0" (echo  Rounds: unlimited ^(Ctrl+C to stop^)) else (echo  Rounds: %ROUNDS%)
if defined DEVICE (echo  Source: Android emulator) else (echo  Source: phone over USB)
echo  Dashboard: http://127.0.0.1:8000
echo ============================================================
echo.

"%PY%" patrol.py --duration %SECS% --rounds %ROUNDS% %DEVICE%!EXTRA!
set "RC=%ERRORLEVEL%"
popd

echo.
echo Patrol finished. The dashboard is still running - close its window to stop it.
pause
exit /b %RC%
