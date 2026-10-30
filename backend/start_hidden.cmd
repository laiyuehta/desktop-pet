@echo off
rem Start the desktop-pet backend (R7: backend\.venv created by backend\setup_venv.ps1; #38). runpy + sys.path also works without the venv.
rem Logs are appended to %PET_BACKEND_LOG_DIR%\backend_out.log / backend_err.log (default = repo root ..\).
rem Keep this file ASCII-only: cmd.exe parses it in the console code page (cp950), UTF-8 comments can break lines.
rem Autostart at logon: Task Scheduler "DesktopPet Backend" -> pythonw backend\autostart\supervise.py -> this file
rem (see PROPOSAL.md "deploy"). Manual remote start without the supervisor:
rem   Invoke-CimMethod Win32_Process -MethodName Create -Arguments @{CommandLine='cmd.exe /c "<repo>\backend\start_hidden.cmd"'}
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if "%PET_ENV_FILE%"=="" set "PET_ENV_FILE=%LOCALAPPDATA%\desktop-pet\env"
if exist "%PET_ENV_FILE%" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%PET_ENV_FILE%") do (
        if not "%%A"=="" if not "%%B"=="" if not defined %%A call set "%%A=%%B"
    )
)
rem Python: default = backend\.venv\Scripts\python.exe; PET_PY overrides; last resort = python on PATH.
if "%PET_PY%"=="" if exist "%~dp0.venv\Scripts\python.exe" set "PET_PY=%~dp0.venv\Scripts\python.exe"
if "%PET_PY%"=="" set PET_PY=python
if "%PET_BACKEND_PORT%"=="" set PET_BACKEND_PORT=8090
cd /d "%~dp0.."
if "%PET_BACKEND_LOG_DIR%"=="" set "PET_BACKEND_LOG_DIR=%CD%"
if not exist "%PET_BACKEND_LOG_DIR%" mkdir "%PET_BACKEND_LOG_DIR%" 2>nul
rem No duplicates: if something already listens on 8090 (or PET_BACKEND_PORT), do not start a second backend.
netstat -ano -p TCP | findstr /R /C:":%PET_BACKEND_PORT% .*LISTENING" >nul && (echo start_hidden: port %PET_BACKEND_PORT% already in use, not starting a second backend & exit /b 0)
"%PET_PY%" -c "import sys,runpy; sys.path.insert(0, r'%CD%'); sys.path.insert(0, r'%CD%\shared'); sys.argv=['backend']; runpy.run_module('backend', run_name='__main__')" 1>>"%PET_BACKEND_LOG_DIR%\backend_out.log" 2>>"%PET_BACKEND_LOG_DIR%\backend_err.log"
