# Create/update the Task Scheduler task "DesktopPet Update" (PROPOSAL.md 16, Issue #14).
# Runs backend\autoupdate.py --once every 5 minutes hidden in background using pythonw.exe.
# Usage (as user who runs the backend, no admin needed):
#   powershell -ExecutionPolicy Bypass -File backend\autostart\install_update_task.ps1
# Uninstall:
#   powershell -ExecutionPolicy Bypass -File backend\autostart\uninstall_update_task.ps1
param([string]$PythonW = '',
      [string]$TaskName = 'DesktopPet Update',
      [int]$IntervalMinutes = 5)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent (Split-Path -Parent $here)
$upPy = Join-Path $root 'backend\autoupdate.py'
$venvW = Join-Path $root 'backend\.venv\Scripts\pythonw.exe'

if (-not $PythonW) { $PythonW = $env:PET_PYTHONW }
if (-not $PythonW -and (Test-Path $venvW)) { $PythonW = $venvW }
if (-not $PythonW -or -not (Test-Path $PythonW)) {
    throw "pythonw.exe not found: $PythonW (run backend\setup_venv.ps1 first to create backend\.venv)"
}

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $PythonW -Argument "`"$upPy`" --once --install-dir `"$root`"" -WorkingDirectory $root
# 定時觸發：登入時啟動，並每 5 分鐘（可自訂）重複執行一次
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)).Repetition

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'desktop-pet R7 autoupdater: checks remote main and CI status, safe backup & in-place update' -Force | Out-Null

$t = Get-ScheduledTask -TaskName $TaskName
"task '$TaskName' state=$($t.State) user=$($t.Principal.UserId) trigger=AtLogOn(repeat ${IntervalMinutes}m) action=$PythonW $upPy"

