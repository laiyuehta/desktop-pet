@echo off
rem Start the desktop-pet backend (R7: ComfyUI embedded Python 3.13; its ._pth ignores PYTHONPATH, so use runpy).
rem Logs are appended to ..\backend_out.log / ..\backend_err.log.
rem Keep this file ASCII-only: cmd.exe parses it in the console code page (cp950), UTF-8 comments can break lines.
rem Autostart at logon: Task Scheduler "DesktopPet Backend" -> pythonw backend\autostart\supervise.py -> this file
rem (see PROPOSAL.md "deploy"). Manual remote start without the supervisor:
rem   Invoke-CimMethod Win32_Process -MethodName Create -Arguments @{CommandLine='cmd.exe /c "<repo>\backend\start_hidden.cmd"'}
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if "%PET_PY%"=="" set PET_PY=C:\path\to\python\python.exe
cd /d "%~dp0.."
rem No duplicates: if something already listens on 8090, do not start a second backend.
netstat -ano -p TCP | findstr /R /C:":8090 .*LISTENING" >nul && (echo start_hidden: port 8090 already in use, not starting a second backend & exit /b 0)
"%PET_PY%" -c "import sys,runpy; sys.path.insert(0, r'%CD%'); sys.argv=['backend']; runpy.run_module('backend', run_name='__main__')" 1>>backend_out.log 2>>backend_err.log
