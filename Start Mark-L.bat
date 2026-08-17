@echo off
REM Start Mark-L.bat — the one supported double-click launcher for JARVIS
REM (Mark-L) on Windows. See docs\WINDOWS_RUNBOOK.md for the full operator
REM runbook (stop/restart/health-check/recovery steps).
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [Mark-L] .venv not found in this folder.
    echo [Mark-L] Create it first — see docs\WINDOWS_RUNBOOK.md, "First-time setup".
    pause
    exit /b 1
)

".venv\Scripts\python.exe" main.py
set EXITCODE=%ERRORLEVEL%

if not "%EXITCODE%"=="0" (
    echo.
    echo [Mark-L] JARVIS exited with code %EXITCODE%. See the messages above,
    echo [Mark-L] and data\logs\jarvis.log, for what happened.
    pause
)

endlocal
