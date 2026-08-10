@echo off
rem Plain-English front door. Double-click it and type what you want, or pass
rem the instruction directly:
rem
rem   run.bat open hik connect and check curebay banamalipur for 1 min
setlocal

set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo.
    echo Could not find the Python environment at:
    echo   %PY%
    echo.
    pause
    exit /b 1
)

pushd "%~dp0"
if "%~1"=="" (
    "%PY%" run.py
) else (
    "%PY%" run.py %*
)
set "RC=%ERRORLEVEL%"
popd

echo.
pause
exit /b %RC%
