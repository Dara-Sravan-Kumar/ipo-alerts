<#
.SYNOPSIS
    Registers Windows Scheduled Tasks for the IPO Alerts daily checks, at
    10:00 AM and 5:00 PM.

.DESCRIPTION
    Must be run from an ELEVATED PowerShell prompt (Right-click PowerShell ->
    "Run as Administrator"). Registering a task that runs whether or not you're
    logged in requires admin rights.

    Creates:
      - IPOAlerts-Morning: runs `python main.py --once` at 10:00 daily
      - IPOAlerts-Evening: runs `python main.py --once` at 17:00 daily
    Both run under your account via S4U logon (no Windows password stored or
    prompted for), whether or not you're logged in.

    Once registered, pin these two in your existing Windows Task Dashboard
    (http://127.0.0.1:8787/) to get a manual "Run" button for each.

    Reliability caveats (real Windows/Task Scheduler limits, not this script):
      - StartWhenAvailable: if the PC was OFF at 10:00/17:00, the missed run
        fires as soon as Windows next starts up. This is the closest real
        equivalent to "run even if it was off."
      - WakeToRun: if the PC is only ASLEEP (not shut down) at the scheduled
        time, Windows wakes it to run the task, then lets it go back to sleep.
      - Neither of these can power on a machine that is fully shut down or
        unplugged — that requires Wake-on-LAN + a BIOS/UEFI RTC wake alarm,
        which is outside what Task Scheduler (or any script) can configure.

.NOTES
    Re-run this script any time to update the tasks (it uses -Force).
    To remove: Unregister-ScheduledTask -TaskName "IPOAlerts-*" -Confirm:$false
#>

$ErrorActionPreference = "Stop"

$RepoPath  = Split-Path -Parent $PSScriptRoot
$PythonExe = (Get-Command python).Source
$User      = "$env:USERDOMAIN\$env:USERNAME"

Write-Host "Repo path : $RepoPath"
Write-Host "Python    : $PythonExe"
Write-Host "User      : $User"
Write-Host ""

$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Limited

$checkSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)

$checkAction = New-ScheduledTaskAction -Execute $PythonExe -Argument "main.py --once" -WorkingDirectory $RepoPath

Register-ScheduledTask -TaskName "IPOAlerts-Morning" -Force `
    -Action $checkAction `
    -Trigger (New-ScheduledTaskTrigger -Daily -At 10:00AM) `
    -Principal $principal -Settings $checkSettings `
    -Description "IPO Alerts daily check (10:00 AM) - python main.py --once"

Register-ScheduledTask -TaskName "IPOAlerts-Evening" -Force `
    -Action $checkAction `
    -Trigger (New-ScheduledTaskTrigger -Daily -At 5:00PM) `
    -Principal $principal -Settings $checkSettings `
    -Description "IPO Alerts daily check (5:00 PM) - python main.py --once"

Write-Host ""
Write-Host "Registered: IPOAlerts-Morning (10:00), IPOAlerts-Evening (17:00)."
Write-Host "Open http://127.0.0.1:8787/ , switch to 'All my tasks', and pin IPOAlerts-Morning / IPOAlerts-Evening for a manual Run button."
