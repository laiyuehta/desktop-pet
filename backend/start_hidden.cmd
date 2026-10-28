@echo off
rem 在背景啟動桌寵後端（R7：用 ComfyUI 內附的 Python 3.13，它的 ._pth 不吃 PYTHONPATH，所以用 runpy）
rem 日誌附加到 ..\backend_out.log / ..\backend_err.log。建議用 WMI 啟動才不會掛在某個主控台底下：
rem   Invoke-CimMethod Win32_Process -MethodName Create -Arguments @{CommandLine='cmd.exe /c "<repo>\backend\start_hidden.cmd"'}
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if "%PET_PY%"=="" set PET_PY=C:\path\to\python\python.exe
cd /d "%~dp0.."
"%PET_PY%" -c "import sys,runpy; sys.path.insert(0, r'%CD%'); sys.argv=['backend']; runpy.run_module('backend', run_name='__main__')" 1>>backend_out.log 2>>backend_err.log
