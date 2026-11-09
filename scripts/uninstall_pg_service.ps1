<#
Undo scripts\install_pg_service.ps1. Run in an elevated PowerShell:

  powershell -ExecutionPolicy Bypass -File .\scripts\uninstall_pg_service.ps1 [-RemoveBinaries]

  1. stop the service EvoWikiPG (Postgres shuts down cleanly, like pg_ctl stop -m fast) and delete it
     (sc delete);
  2. remove NetworkService's grants from the data dir, the log dir and .pg18;
  3. with -RemoveBinaries, delete <main>\.pg18 (refused while a postgres.exe from it still runs,
     e.g. the long-lived test cluster).

Kept as they are: the data dir, .env (its port stays; the WMI path uses whatever port .env names),
<data>\evowiki_service.conf (the WMI-started server keeps logging into .pglog). Start the dev DB
the old way afterwards with EvoWiki.exe (no admin). Idempotent: missing pieces are skipped.
#>
param(
    [string]$Main,
    [string]$DataDir,
    [string]$BinRoot,
    [string]$LogDir,
    [string]$ServiceName = 'EvoWikiPG',
    [string]$Account = 'NT AUTHORITY\NetworkService',
    [switch]$RemoveBinaries
)
$ErrorActionPreference = 'Stop'

function Fail([string]$msg) { Write-Host "失败：$msg" -ForegroundColor Red; exit 1 }
function Say([string]$step, [string]$result, [string]$detail = '') {
    if ($detail) { Write-Host ("[{0}] {1} - {2}" -f $step, $result, $detail) } else { Write-Host ("[{0}] {1}" -f $step, $result) }
}

$p = New-Object Security.Principal.WindowsPrincipal ([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail '需要管理员权限：右键 PowerShell →“以管理员身份运行”，再运行本脚本'
}
if (-not $Main) {
    $ErrorActionPreference = 'Continue'
    $common = (& git -C $PSScriptRoot rev-parse --path-format=absolute --git-common-dir 2>$null)
    $ErrorActionPreference = 'Stop'
    if ($LASTEXITCODE -eq 0 -and $common) { $Main = Split-Path -Parent $common.Trim() }
    elseif (Test-Path (Join-Path $PSScriptRoot '..\.git') -PathType Container) { $Main = Join-Path $PSScriptRoot '..' }
    else { Fail '找不到主仓库；用 -Main <checkout> 指定' }
}
$Main = [IO.Path]::GetFullPath($Main)
if (-not $DataDir) { $DataDir = Join-Path $Main '.pgdata' }
if (-not $BinRoot) { $BinRoot = Join-Path $Main '.pg18' }
if (-not $LogDir) { $LogDir = Join-Path $Main '.pglog' }

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc) {
    if ($svc.Status -ne 'Stopped') {
        try { Stop-Service -Name $ServiceName } catch { Fail "停止服务 $ServiceName 失败：$($_.Exception.Message)" }
        Say '1/3 服务' '已停止' $ServiceName
    }
    & sc.exe delete $ServiceName | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "sc delete $ServiceName 失败（退出码 $LASTEXITCODE）" }
    for ($i = 0; $i -lt 20 -and (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Milliseconds 500 }
    Say '1/3 服务' '已删除' $ServiceName
} else {
    Say '1/3 服务' '跳过' "没有服务 $ServiceName"
}

$sid = (New-Object Security.Principal.NTAccount $Account).Translate([Security.Principal.SecurityIdentifier]).Value
foreach ($d in $DataDir, $LogDir, $BinRoot) {
    if (Test-Path $d) {
        & icacls $d /remove:g "*$sid" /T /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail "icacls $d 撤销授权失败（退出码 $LASTEXITCODE）" }
    }
}
Say '2/3 权限' '已撤销' "$Account 在 $DataDir、$LogDir、$BinRoot 上的授权"

if ($RemoveBinaries -and (Test-Path $BinRoot)) {
    $full = [IO.Path]::GetFullPath($BinRoot)
    $inUse = @(Get-Process -Name postgres, pg_ctl -ErrorAction SilentlyContinue |
               Where-Object { $_.Path -and $_.Path.StartsWith($full + '\', [StringComparison]::OrdinalIgnoreCase) })
    if ($inUse.Count -gt 0) { Fail "$full 里的二进制还在运行（pid $(($inUse | ForEach-Object { $_.Id }) -join ', ')），没有删除" }
    Remove-Item -Recurse -Force $full
    Say '3/3 二进制' '已删除' $full
} else {
    Say '3/3 二进制' '保留' "$BinRoot（加 -RemoveBinaries 删除）"
}
Write-Host ''
Write-Host '完成。用原来的方式启动开发库：双击 EvoWiki.exe（不需要管理员）。' -ForegroundColor Green
