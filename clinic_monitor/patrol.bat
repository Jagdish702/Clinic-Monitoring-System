@echo off
rem Continuous patrol: rotate through every clinic, one after another.
rem
rem   patrol.bat              60 seconds per clinic (default), runs until Ctrl+C
rem   patrol.bat 120          two minutes per clinic
rem   patrol.bat 90 3         90 seconds each, stop after 3 full rounds
rem
rem Anything else can be passed straight through, for example:
rem   patrol.bat 60 0 --clinics BANAMALIPUR,NAYAHAT
rem   patrol.bat 60 0 --all-cameras
setlocal

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

rem First argument: seconds per clinic. Second: number of rounds (0 = forever).
set "SECS=%~1"
if "%SECS%"=="" set "SECS=60"
set "ROUNDS=%~2"
if "%ROUNDS%"=="" set "ROUNDS=0"

rem Pass any remaining arguments through untouched.
set "EXTRA="
if not "%~3"=="" (
    shift
    shift
    :collect
    if "%~1"=="" goto done
    set "EXTRA=%EXTRA% %1"
    shift
    goto collect
)
:done

echo.
echo Patrolling every clinic - %SECS% seconds each.
if "%ROUNDS%"=="0" (echo Rounds: unlimited ^(press Ctrl+C to stop^)) else (echo Rounds: %ROUNDS%)
echo.

pushd "%~dp0"
"%PY%" patrol.py --duration %SECS% --rounds %ROUNDS%%EXTRA%
set "RC=%ERRORLEVEL%"
popd

echo.
pause
exit /b %RC%
