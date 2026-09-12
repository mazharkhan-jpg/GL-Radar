# Windows: start GL Radar at logon and keep it running.
# Run once in PowerShell as Administrator, after editing $AppDir.

$AppDir = "C:\Users\YOU\gl-radar"
$Python = Join-Path $AppDir ".venv\Scripts\pythonw.exe"

$action  = New-ScheduledTaskAction -Execute $Python `
           -Argument "-m radar.cli serve" -WorkingDirectory $AppDir
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
            -StartWhenAvailable -DontStopOnIdleEnd -ExecutionTimeLimit 0

Register-ScheduledTask -TaskName "GL Radar" -Action $action -Trigger $trigger `
  -Settings $settings -Description "Portfolio monitoring for Gross Labs" -Force

Write-Host "Registered. Open http://127.0.0.1:8787"
Write-Host "StartWhenAvailable means a missed run fires once the machine is back on."
