@echo off
rem DesktopPet backend launcher for Windows (see backend\README.md)
rem Usage: backend\run_backend.bat [--port 8090] [--config path\to\config.json] [-v]
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m backend %*
) else (
    python -m backend %*
)
if errorlevel 1 (
    echo.
    echo [backend] exited with error. Is Python 3.10+ installed? Try: py -3 --version
    pause
)
endlocal
