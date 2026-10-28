# Create/update the Task Scheduler task "DesktopPet Backend" (runs at logon of the current user, hidden).
# Usage (as pi-user, no admin needed):  powershell -ExecutionPolicy Bypass -File backend\autostart\install_task.ps1
# Disable/remove:                          powershell -ExecutionPolicy Bypass -File backend\autostart\uninstall_task.ps1
param([string]$PythonW = 'C:\path\to\python\pythonw.exe',
      [string]$TaskName = 'DesktopPet Backend')
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$sup = Join-Path $here 'supervise.py'
$root = Split-Path -Parent (Split-Path -Parent $here)
if (-not (Test-Path $PythonW)) { throw "pythonw.exe not found: $PythonW" }
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
