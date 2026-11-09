<#
EvoWiki start-at-login (optional). Registers a per-user Task Scheduler task that runs
EvoWiki.exe 30 s after you log on. Desktop logs are written to
<main checkout>\.evowiki-run\desktop.log.

  powershell -File scripts\evowiki_autostart.ps1 -Enable
  powershell -File scripts\evowiki_autostart.ps1 -Disable
  powershell -File scripts\evowiki_autostart.ps1          # status

Only the current user's task "\EvoWiki\Start" is created or removed; no admin rights needed.
Stop the running services with the tray menu or the CLI down command (disabling autostart does not stop them).
#>
param([switch]$Enable, [switch]$Disable)
$ErrorActionPreference = 'Stop'
$TaskPath = '\EvoWiki\'
$TaskName = 'Start'

# the main checkout (also when this script sits in a git worktree)
$common = (& git -C $PSScriptRoot rev-parse --path-format=absolute --git-common-dir).Trim()
$Main = Split-Path -Parent $common
$Start = Join-Path $Main 'EvoWiki.exe'
$Log = Join-Path $Main '.evowiki-run\desktop.log'

function Get-EvoTask { Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue }

if ($Enable -and $Disable) { throw 'use -Enable or -Disable, not both' }

if ($Enable) {
    if (-not (Test-Path $Start)) { throw "$Start not found: build the desktop application in the main checkout first" }
    $action = New-ScheduledTaskAction -Execute $Start -WorkingDirectory $Main
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $trigger.Delay = 'PT30S'
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force `
        -Description 'EvoWiki: start the desktop tray and local learning service at logon' | Out-Null
    Write-Output "开机自启：已开启（登录 30 秒后运行 $Start，日志 $Log）"
}
elseif ($Disable) {
    if (Get-EvoTask) {
        Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Confirm:$false
        Write-Output '开机自启：已关闭（正在运行的服务不受影响，停止请用托盘菜单或 CLI down 命令）'
    } else {
        Write-Output '开机自启：本来就没开'
    }
}
else {
    $t = Get-EvoTask
    if ($t) { Write-Output "开机自启：已开启（$($t.State)，运行 $Start）" } else { Write-Output '开机自启：未开启' }
}
