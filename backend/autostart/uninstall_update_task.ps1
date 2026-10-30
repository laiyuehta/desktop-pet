# Unregister Task Scheduler task "DesktopPet Update" (PROPOSAL.md 16, Issue #14).
param([string]$TaskName = 'DesktopPet Update')
$ErrorActionPreference = 'SilentlyContinue'
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
"task '$TaskName' removed"
