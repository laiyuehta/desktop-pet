# Remove the "DesktopPet Backend" task. With -Stop also stops the running supervisor and the backend on port 8090.
param([string]$TaskName = 'DesktopPet Backend', [switch]$Stop)
$ErrorActionPreference = 'Continue'
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    "removed task '$TaskName'"
} else { "task '$TaskName' not found" }
if ($Stop) {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*autostart\supervise.py*' -or ($_.CommandLine -like "*run_module('backend'*" -and $_.CommandLine -like '*desktop-pet*') } |
        ForEach-Object { "stopping PID $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force }
}
