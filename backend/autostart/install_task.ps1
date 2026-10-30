# Create/update the Task Scheduler task "DesktopPet Backend" (runs at logon of the current user, hidden).
# Usage (as the user who runs the backend, no admin needed):  powershell -ExecutionPolicy Bypass -File backend\autostart\install_task.ps1
# Disable/remove:                          powershell -ExecutionPolicy Bypass -File backend\autostart\uninstall_task.ps1
# Run backend\setup_venv.ps1 first (creates backend\.venv; #38: no longer uses the ComfyUI embedded python).
# -PythonW: pythonw.exe to run supervise.py. Default: backend\.venv\Scripts\pythonw.exe, then $env:PET_PYTHONW.
param([string]$PythonW = '',
      [string]$TaskName = 'DesktopPet Backend')
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$sup = Join-Path $here 'supervise.py'
$root = Split-Path -Parent (Split-Path -Parent $here)
$venvW = Join-Path $root 'backend\.venv\Scripts\pythonw.exe'
if (-not $PythonW -and (Test-Path $venvW)) { $PythonW = $venvW }
if (-not $PythonW) { $PythonW = $env:PET_PYTHONW }
if (-not $PythonW -or -not (Test-Path $PythonW)) { throw "pythonw.exe not found: $PythonW (run backend\setup_venv.ps1 first to create backend\.venv)" }
$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $PythonW -Argument "`"$sup`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'desktop-pet backend (port 8090): pythonw supervise.py -> start_hidden.cmd, restarts on exit, skips if 8090 already in use' -Force | Out-Null
$t = Get-ScheduledTask -TaskName $TaskName
"task '$TaskName' state=$($t.State) user=$($t.Principal.UserId) trigger=AtLogOn action=$PythonW $sup"
