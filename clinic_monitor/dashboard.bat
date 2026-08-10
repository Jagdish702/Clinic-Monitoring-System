@echo off
rem Serve the alert dashboard at http://127.0.0.1:8000 (Ctrl+C to stop).
setlocal

set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo Could not find the Python environment at "%PY%".
    pause
    exit /b 1
)

pushd "%~dp0"
echo Dashboard starting - open http://127.0.0.1:8000 in your browser.
echo Press Ctrl+C here to stop it.
"%PY%" dashboard\app.py
popd
pause
