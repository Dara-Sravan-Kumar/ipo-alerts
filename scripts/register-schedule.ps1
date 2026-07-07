<#
.SYNOPSIS
    Registers ONE Windows Scheduled Task ("IPOAlerts") for the IPO Alerts
    daily checks, firing at both 10:00 AM and 5:00 PM.

.DESCRIPTION
    No admin rights needed — registers under your own account with an
    Interactive logon (a standard user can always create their own tasks this
    way; only "run whether logged on or not" needs elevation).

    Creates a single task, "IPOAlerts", with two daily triggers (10:00 and
    17:00), both running `python main.py --digest`.

    --digest (not --once) is intentional: it re-fetches and re-posts the
    CURRENT set of live/upcoming IPOs every run, with no dedup gating. A newly
    announced IPO appears, an IPO that has since closed drops off (the facts
    fetch's own date filter excludes it), and an unchanged IPO gets posted
    again identically. --once instead only alerts once per genuinely new
    dedup event and stays silent otherwise -- not what a "check what's active
    right now" schedule should do.

    Clicking "Run" on the task (e.g. from the Windows Task Dashboard at
    http://127.0.0.1:8787/) always fires it immediately — that's inherent to
    Task Scheduler and doesn't touch or disable the 10:00/17:00 triggers,
    which keep firing on their own regardless of any manual runs.

    Every run — scheduled or manual — appends to ipo_tracker.log next to this
    script's parent folder, so you can always confirm a run actually happened
    even when it found nothing new to alert on (which looks like "nothing
    happened" from Discord alone, since already-alerted IPOs are correctly
    skipped and produce no message).

    Reliability caveats (real Windows/Task Scheduler limits, not this script):
      - Interactive logon: the task only fires while you're logged in (locked
        screen still counts as logged in; fully logged out does not).
      - StartWhenAvailable: if the PC was off (or you were logged out) at
        10:00/17:00, the missed run fires as soon as you next log in /
        Windows starts up. This is the "runs when the system turns on" part.
      - WakeToRun: if the PC is only ASLEEP (not shut down) at the scheduled
        time, Windows wakes it to run the task, then lets it go back to sleep.
      - Nothing here can power on a machine that is fully shut down or
        unplugged — that needs Wake-on-LAN + a BIOS/UEFI RTC wake alarm, which
        is outside what Task Scheduler (or any script) can configure.

.NOTES
    Re-run this script any time to update the task (it uses -Force).
    To remove: Unregister-ScheduledTask -TaskName "IPOAlerts" -Confirm:$false
#>

$ErrorActionPreference = "Stop"

$RepoPath  = Split-Path -Parent $PSScriptRoot
$PythonExe = (Get-Command python).Source
$User      = "$env:USERDOMAIN\$env:USERNAME"

Write-Host "Repo path : $RepoPath"
Write-Host "Python    : $PythonExe"
Write-Host "User      : $User"
Write-Host ""

# Clean up the old two-task layout from an earlier version of this script.
Get-ScheduledTask -TaskName "IPOAlerts-Morning", "IPOAlerts-Evening" -ErrorAction SilentlyContinue |
    Unregister-ScheduledTask -Confirm:$false

$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

$action = New-ScheduledTaskAction -Execute $PythonExe -Argument "main.py --digest" -WorkingDirectory $RepoPath

$triggers = @(
    New-ScheduledTaskTrigger -Daily -At 10:00AM
    New-ScheduledTaskTrigger -Daily -At 5:00PM
)

Register-ScheduledTask -TaskName "IPOAlerts" -Force `
    -Action $action `
    -Trigger $triggers `
    -Principal $principal -Settings $settings `
    -Description "IPO Alerts daily check, 10:00 AM and 5:00 PM - python main.py --digest"

Write-Host ""
Write-Host "Registered: IPOAlerts (10:00 and 17:00 daily)."
Write-Host "Open http://127.0.0.1:8787/ , switch to 'All my tasks', and pin IPOAlerts for a manual Run button."
Write-Host "Every run logs to ipo_tracker.log in the repo folder."
