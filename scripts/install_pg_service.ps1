<#
EvoWiki dev Postgres as a Windows service (reuse audit B8). Run ONCE in an elevated PowerShell
(right-click PowerShell -> Run as administrator):

  powershell -ExecutionPolicy Bypass -File .\scripts\install_pg_service.ps1

Idempotent: every step that is already done is skipped, so running it again is safe. Any failure
stops at once with a Chinese message (fail closed); nothing after the failed step runs.

  1. binaries  copy Postgres 18 out of <main>\.venv (pixeltable_pgserver\pginstall18: bin, lib, share,
               include) to <main>\.pg18, so `uv sync` can never replace a running server's files;
               put libwinpthread-1.dll next to postgres.exe (the only non-Windows DLL postgres.exe
               and the extensions import, checked with ldd; taken from Git for Windows'
               reported runtime bin directory); check postgres / pg_ctl / initdb / psql -V with a clean PATH (System32
               only), which is all a service gets.
  2. logging   write <data>\evowiki_service.conf and include it from postgresql.conf:
               logging_collector into <main>\.pglog (outside the data dir: an open log inside it
               made crash recovery's fsync hit sharing violations), one file per weekday
               (postgresql-Mon.log ...), rotated daily and truncated when its weekday comes round,
               so at most 7 files (about a week). Postgres has no keep-N for size-based rotation,
               so there is no size cap within a day.
  3. port      check <port> (default 15432) is outside Windows' dynamic port range and every
               excluded range (WinNAT/Hyper-V reserve blocks inside the dynamic range at boot) and
               that nothing else listens on it.
  4. register  pg_ctl register -N EvoWikiPG -U "NT AUTHORITY\NetworkService" -D <data> -S auto -w
               -o "-h 127.0.0.1 -p <port>", restart-on-failure, and grant the desktop user
               start / stop / query rights on the service (sc sdset), so EvoWiki.exe
               (`evowiki_ctl up`) can start it and `down --db` stop it WITHOUT admin.
  5. stop old  stop the server currently running on the data dir (the WMI-started one) cleanly:
               pg_ctl stop -m fast. Other sessions' connections drop here.
  6. ACLs      NetworkService gets Modify on the data dir and the log dir, Read+Execute on .pg18.
  7. start     Start-Service, then SELECT 1 and CREATE EXTENSION IF NOT EXISTS vector (in a
               transaction that is rolled back) through the copied psql.
  8. .env      set the port of EVOWIKI_DATABASE_URL in <main>\.env to <port>; every other byte of
               the file is kept, no value is printed.

Service account: NT AUTHORITY\NetworkService - built in, no password to store or expire, not an
administrator (Postgres refuses to run as one), and the account the EDB installer uses. It only
gets rights on the three directories above.

Undo: scripts\uninstall_pg_service.ps1 (also as admin).

Testing aids (no admin needed, no service touched):
  -CopyOnly   -BinRoot <dir>                    only step 1, into <dir>
  -ConfigOnly -DataDir <dir> -LogDir <dir>      only step 2
  -EnvOnly    -EnvFile <file> [-Port N]         only step 8, on <file>
#>
param(
    [string]$Main,
    [string]$DataDir,
    [string]$BinRoot,
    [string]$LogDir,
    [int]$Port = 15432,
    [string]$ServiceName = 'EvoWikiPG',
    [string]$Account = 'NT AUTHORITY\NetworkService',
    [string]$GrantUser,
    [switch]$CopyOnly,
    [switch]$ConfigOnly,
    [switch]$EnvOnly,
    [string]$EnvFile
)
$ErrorActionPreference = 'Stop'
$ConfName = 'evowiki_service.conf'
$Utf8NoBom = New-Object System.Text.UTF8Encoding $false

function Fail([string]$msg) {
    Write-Host "失败：$msg" -ForegroundColor Red
    exit 1
}
function Say([string]$step, [string]$result, [string]$detail = '') {
    if ($detail) { Write-Host ("[{0}] {1} - {2}" -f $step, $result, $detail) } else { Write-Host ("[{0}] {1}" -f $step, $result) }
}

function Resolve-Main {
    # the main checkout: the parent of the common .git dir (also when this script sits in a worktree)
    $ErrorActionPreference = 'Continue'
    try {
        $common = (& git -C $PSScriptRoot rev-parse --path-format=absolute --git-common-dir 2>$null)
        if ($LASTEXITCODE -eq 0 -and $common) { return [IO.Path]::GetFullPath((Split-Path -Parent $common.Trim())) }
    } catch { }
    $up = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    if (Test-Path (Join-Path $up '.git') -PathType Container) { return $up }
    Fail "找不到主仓库（git 不可用，且 $up 不是主仓库）；用 -Main <checkout> 指定"
}

function Test-Admin {
    $p = New-Object Security.Principal.WindowsPrincipal ([Security.Principal.WindowsIdentity]::GetCurrent())
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-Sid([string]$name) {
    try { return (New-Object Security.Principal.NTAccount $name).Translate([Security.Principal.SecurityIdentifier]).Value }
    catch { Fail "账户 $name 不存在：$($_.Exception.Message)" }
}

# ---------- 1. binaries ----------

function Find-WinPthread {
    $git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $git) { Fail '请安装完整官方 Git for Windows 并重新打开终端' }
    $execPath = & $git.Source --exec-path
    if ($LASTEXITCODE -eq 0 -and $execPath) {
        $prefix = Split-Path -Parent (Split-Path -Parent $execPath.Trim())
        $runtime = Join-Path $prefix 'bin\libwinpthread-1.dll'
        if (Test-Path $runtime -PathType Leaf) { return $runtime }
    }
    Fail '官方 Git for Windows 缺少 libwinpthread-1.dll；请重新安装完整 Git 并运行 doctor'
}

function Invoke-CleanPath([string]$exe, [string]$arg) {
    # run with a PATH of System32 only, like a service; returns @(exitcode, stdout)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    $psi.Arguments = $arg
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.EnvironmentVariables['PATH'] = "$env:SystemRoot\System32;$env:SystemRoot"
    $p = [System.Diagnostics.Process]::Start($psi)
    $out = $p.StandardOutput.ReadToEnd() + $p.StandardError.ReadToEnd()
    $p.WaitForExit()
    return @($p.ExitCode, $out.Trim())
}

function Test-Binaries([string]$bin) {
    foreach ($exe in 'postgres', 'pg_ctl', 'initdb', 'psql') {
        $r = Invoke-CleanPath (Join-Path $bin "$exe.exe") '-V'
        if ($r[0] -ne 0 -or $r[1] -notmatch '\(PostgreSQL\) 18') {
            Fail ("{0}.exe -V 在干净的 PATH 下失败（退出码 {1}，输出 {2}）；缺 DLL 时退出码是 -1073741515" -f $exe, $r[0], $r[1])
        }
    }
    return (Invoke-CleanPath (Join-Path $bin 'postgres.exe') '-V')[1]
}

function Get-SourceStamp([string]$src) {
    $h = @('bin\postgres.exe', 'bin\pg_ctl.exe', 'lib\postgresql\vector.dll') | ForEach-Object {
        (Get-FileHash -Algorithm SHA256 (Join-Path $src $_)).Hash
    }
    return ($h -join ',')
}

function Copy-PgBinaries([string]$src, [string]$dest) {
    foreach ($need in 'bin\postgres.exe', 'bin\pg_ctl.exe', 'lib\postgresql\vector.dll', 'share') {
        if (-not (Test-Path (Join-Path $src $need))) { Fail "$src 里没有 $need：先在主仓库运行 uv sync" }
    }
    $stamp = Get-SourceStamp $src
    $stampFile = Join-Path $dest '.evowiki-source'
    if ((Test-Path $stampFile) -and ((Get-Content $stampFile -Raw).Trim() -eq $stamp) -and
        (Test-Path (Join-Path $dest 'bin\libwinpthread-1.dll'))) {
        $v = Test-Binaries (Join-Path $dest 'bin')
        Say '1/8 二进制' '跳过' "$dest 已是 .venv 里同一版本（$v）"
        return
    }
    $dll = Find-WinPthread
    $staging = "$dest.new"
    if (Test-Path $staging) { Remove-Item -Recurse -Force $staging }
    New-Item -ItemType Directory -Force $staging | Out-Null
    Copy-Item -Path (Join-Path $src '*') -Destination $staging -Recurse -Force
    Copy-Item -Path $dll -Destination (Join-Path $staging 'bin') -Force
    $v = Test-Binaries (Join-Path $staging 'bin')
    if (Test-Path $dest) {
        $inUse = @(Get-Process -Name postgres, pg_ctl -ErrorAction SilentlyContinue |
                   Where-Object { $_.Path -and $_.Path.StartsWith($dest + '\', [StringComparison]::OrdinalIgnoreCase) })
        if ($inUse.Count -gt 0) {
            Remove-Item -Recurse -Force $staging
            Fail ("{0} 里的二进制正在运行（pid {1}），不能替换：先停掉它们（服务 Stop-Service {2}；测试集群 pg_ctl -D <目录> stop）再运行" -f
                  $dest, (($inUse | ForEach-Object { $_.Id }) -join ', '), $ServiceName)
        }
        try { Remove-Item -Recurse -Force $dest } catch { Fail "删不掉旧的 ${dest}：$($_.Exception.Message)" }
    }
    Rename-Item -Path $staging -NewName (Split-Path -Leaf $dest)
    [IO.File]::WriteAllText($stampFile, $stamp, $Utf8NoBom)
    Say '1/8 二进制' '已复制' "$src -> $dest（$v），libwinpthread-1.dll 来自 $(Split-Path -Parent $dll)"
}

# ---------- 2. logging ----------

function Write-LoggingConf([string]$data, [string]$logs) {
    # returns $true when the server needs a restart to pick the change up
    if (-not (Test-Path (Join-Path $data 'postgresql.conf'))) { Fail "$data 里没有 postgresql.conf，不是 Postgres 数据目录" }
    New-Item -ItemType Directory -Force $logs | Out-Null
    $logDirConf = ([IO.Path]::GetFullPath($logs)) -replace '\\', '/'
    $text = @(
        '# EvoWiki: written by scripts/install_pg_service.ps1 (re-running it rewrites this file)',
        'logging_collector = on',
        "log_directory = '$logDirConf'",
        "log_filename = 'postgresql-%a.log'",
        'log_rotation_age = 1d',
        'log_rotation_size = 0',
        'log_truncate_on_rotation = on',
        "log_line_prefix = '%m [%p] '",
        ''
    ) -join "`n"
    $confFile = Join-Path $data $ConfName
    $changed = $false
    if (-not (Test-Path $confFile) -or ([IO.File]::ReadAllText($confFile) -ne $text)) {
        [IO.File]::WriteAllText($confFile, $text, $Utf8NoBom)
        $changed = $true
    }
    $main = Join-Path $data 'postgresql.conf'
    $line = "include_if_exists = '$ConfName'"
    $body = [IO.File]::ReadAllText($main)
    if (-not $body.Contains($line)) {
        [IO.File]::WriteAllText($main, $body.TrimEnd("`r", "`n") + "`n`n# EvoWiki`n$line`n", $Utf8NoBom)
        $changed = $true
    }
    if ($changed) { Say '2/8 日志' '已写入' "$confFile（日志在 $logs，每个星期几一个文件，最多 7 个）" }
    else { Say '2/8 日志' '跳过' "$confFile 已是最新" }
    return $changed
}

# ---------- 3. port ----------

function Test-PortUsable([int]$port) {
    $dyn = [regex]::Matches((netsh int ipv4 show dynamicport tcp | Out-String), ':\s*(\d+)')
    if ($dyn.Count -ge 2) {
        $start = [int]$dyn[0].Groups[1].Value; $count = [int]$dyn[1].Groups[1].Value
        if ($port -ge $start -and $port -lt $start + $count) {
            Fail "端口 $port 在 Windows 动态端口范围 $start–$($start + $count - 1) 里（WinNAT 可能保留它）；用 -Port 选一个范围外的端口"
        }
    }
    foreach ($m in [regex]::Matches((netsh interface ipv4 show excludedportrange protocol=tcp | Out-String), '(?m)^\s*(\d+)\s+(\d+)')) {
        $a = [int]$m.Groups[1].Value; $b = [int]$m.Groups[2].Value
        if ($port -ge $a -and $port -le $b) { Fail "端口 $port 被 Windows 保留（$a–$b）；用 -Port 选别的端口，或 net stop winnat / net start winnat 后再运行" }
    }
    $l = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
    if ($l.Count -gt 0) {
        $owner = ($l | ForEach-Object { $_.OwningProcess } | Sort-Object -Unique) -join ', '
        Fail "端口 $port 已被进程 $owner 占用；先停掉它，或用 -Port 选别的端口"
    }
    Say '3/8 端口' 'OK' "$port 不在动态 / 保留范围里，空闲"
}

# ---------- 4. register ----------

function Get-ServiceRow([string]$name) {
    return Get-CimInstance Win32_Service -Filter "Name='$name'" -ErrorAction SilentlyContinue
}

function Test-ServiceMatches($row, [string]$pgctl, [string]$data, [int]$port, [string]$account) {
    # pg_ctl register writes canonical paths (forward slashes): compare with backslashes, case-insensitively
    $p = $row.PathName.Replace('/', '\').ToLower()
    $okBin = $p.Contains(($pgctl -replace '\.exe$', '').ToLower())
    $okData = $p.Contains(('-D "{0}"' -f $data).ToLower())
    $okPort = $p -match ("-p {0}(\D|$)" -f $port)
    $okAcct = ($row.StartName -replace '\s', '').ToLower() -eq ($account -replace '\s', '').ToLower()
    return ($okBin -and $okData -and $okPort -and $okAcct)
}

function Register-PgService([string]$bin, [string]$data, [int]$port) {
    $pgctl = Join-Path $bin 'pg_ctl.exe'
    $row = Get-ServiceRow $ServiceName
    if ($row) {
        if (-not (Test-ServiceMatches $row $pgctl $data $port $Account)) {
            Fail ("已有服务 {0}，但配置不同（{1}，账户 {2}）：先运行 scripts\uninstall_pg_service.ps1 再运行本脚本" -f
                  $ServiceName, $row.PathName, $row.StartName)
        }
        Say '4/8 服务' '跳过' "$ServiceName 已注册（$($row.State)）"
    } else {
        & $pgctl register -N $ServiceName -U $Account -D $data -S auto -w -t 300 -o "-h 127.0.0.1 -p $port"
        if ($LASTEXITCODE -ne 0) { Fail "pg_ctl register 失败（退出码 $LASTEXITCODE）" }
        $row = Get-ServiceRow $ServiceName
        if (-not $row -or -not (Test-ServiceMatches $row $pgctl $data $port $Account)) {
            Fail "pg_ctl register 返回成功，但服务配置不对：$($row.PathName)"
        }
        & sc.exe description $ServiceName 'EvoWiki dev Postgres (scripts/install_pg_service.ps1)' | Out-Null
        Say '4/8 服务' '已注册' "$ServiceName：$($row.PathName)，账户 $Account，自动启动"
    }
    & sc.exe failure $ServiceName reset= 86400 actions= restart/60000/restart/60000// | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "sc failure（崩溃后自动重启）设置失败（退出码 $LASTEXITCODE）" }
    Grant-ServiceControl
}

function Grant-ServiceControl {
    $user = $GrantUser
    if (-not $user) { $user = (Get-CimInstance Win32_ComputerSystem).UserName }   # the desktop user, also when elevated as another admin
    if (-not $user) { Fail '读不到当前登录的桌面用户；用 -GrantUser <用户名> 指定' }
    $sid = Get-Sid $user
    $sddl = ((& sc.exe sdshow $ServiceName) | Where-Object { $_ -match '^D:' }) -join ''
    if (-not $sddl) { Fail "sc sdshow $ServiceName 读不到权限" }
    $ace = "(A;;CCLCSWRPWPLORC;;;$sid)"
    if ($sddl.Contains($ace)) { Say '4/8 权限' '跳过' "$user 已能启停 $ServiceName"; return }
    $i = $sddl.IndexOf(')S:')
    $new = if ($i -ge 0) { $sddl.Insert($i + 1, $ace) } else { $sddl + $ace }
    & sc.exe sdset $ServiceName $new | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "sc sdset 失败（退出码 $LASTEXITCODE）" }
    Say '4/8 权限' '已授予' "$user 可以不用管理员启动 / 停止 $ServiceName"
}

# ---------- 5. stop the old server ----------

function Stop-OldServer([string]$bin, [string]$data) {
    $pidFile = Join-Path $data 'postmaster.pid'
    if (-not (Test-Path $pidFile)) { Say '5/8 旧服务器' '跳过' '没在运行'; return }
    $pmPid = [int]((Get-Content $pidFile -TotalCount 1).Trim())
    $proc = Get-Process -Id $pmPid -ErrorAction SilentlyContinue
    if (-not $proc -or $proc.ProcessName -ne 'postgres') { Say '5/8 旧服务器' '跳过' "postmaster.pid 是旧的（pid $pmPid 不在运行）"; return }
    $svc = Get-ServiceRow $ServiceName
    if ($svc -and $svc.State -eq 'Running' -and $svc.ProcessId -gt 0) {
        $parent = (Get-CimInstance Win32_Process -Filter "ProcessId=$pmPid" -ErrorAction SilentlyContinue).ParentProcessId
        if ($parent -eq $svc.ProcessId) { Say '5/8 旧服务器' '跳过' "数据目录由服务 $ServiceName 运行"; return }
    }
    & (Join-Path $bin 'pg_ctl.exe') stop -D $data -m fast -w -t 120
    if ($LASTEXITCODE -ne 0) { Fail "pg_ctl stop -m fast 失败（退出码 $LASTEXITCODE）；旧服务器（pid $pmPid）还在运行，没有做后面的步骤" }
    Say '5/8 旧服务器' '已停止' "pid $pmPid（pg_ctl stop -m fast）"
}

# ---------- 6. ACLs ----------

function Grant-Acl([string]$path, [string]$sid, [string]$rights) {
    & icacls $path /grant "*${sid}:(OI)(CI)$rights" /T /C /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "icacls $path 授权失败（退出码 $LASTEXITCODE）" }
}

# ---------- 7. start + verify ----------

function Get-EnvUrl([string]$envPath) {
    if (-not (Test-Path $envPath)) { Fail "$envPath 不存在" }
    $m = [regex]::Match([IO.File]::ReadAllText($envPath), '(?m)^\s*(?:export\s+)?EVOWIKI_DATABASE_URL\s*=\s*["'']?([^\r\n"'']+)')
    if (-not $m.Success) { Fail "$envPath 里没有 EVOWIKI_DATABASE_URL" }
    return $m.Groups[1].Value.Trim()
}

function Test-Database([string]$bin, [int]$port, [string]$url) {
    $ErrorActionPreference = 'Continue'     # 5.1: psql's stderr under 2>&1 must not throw; exit codes are checked
    $u = [regex]::Match($url, '^[a-z]+://(?:([^:@/]*)(?::([^@/]*))?@)?[^/:]+(?::\d+)?/?([^?]*)')
    $user = if ($u.Groups[1].Value) { [Uri]::UnescapeDataString($u.Groups[1].Value) } else { 'postgres' }
    $db = if ($u.Groups[3].Value) { [Uri]::UnescapeDataString($u.Groups[3].Value) } else { 'postgres' }
    if ($u.Groups[2].Value) { $env:PGPASSWORD = [Uri]::UnescapeDataString($u.Groups[2].Value) }
    $psql = Join-Path $bin 'psql.exe'
    $one = (& $psql -X -w -h 127.0.0.1 -p $port -U $user -d $db -tAc 'select 1' 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $one -ne '1') { Fail "SELECT 1 失败（用户 $user，库 $db）：$one" }
    $ext = (& $psql -X -q -w -v ON_ERROR_STOP=1 -h 127.0.0.1 -p $port -U $user -d $db -tAc `
            "begin; create extension if not exists vector; select extversion from pg_extension where extname = 'vector'; rollback;" 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { Fail "CREATE EXTENSION vector 失败：$ext" }
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    $ver = ($ext -split "`r?`n" | Where-Object { $_ -match '^\d' } | Select-Object -First 1)
    Say '7/8 检查' 'OK' "SELECT 1 通过；pgvector $ver 可用（库 $db，检查用的事务已回滚）"
}

function Start-PgService([string]$bin, [int]$port, [string]$url, [bool]$restart) {
    $svc = Get-Service -Name $ServiceName
    try {
        if ($svc.Status -eq 'Running' -and $restart) { Restart-Service -Name $ServiceName; $how = '已重启（日志配置有变化）' }
        elseif ($svc.Status -eq 'Running') { $how = '跳过（已在运行）' }
        else { Start-Service -Name $ServiceName; $how = '已启动' }
    } catch {
        Fail ("服务 {0} 启动失败：{1}`n  看 {2} 里的日志和事件查看器（Windows 日志 → 应用程序，来源 PostgreSQL）；" -f
              $ServiceName, $_.Exception.Message, $LogDir) +
             "要回到原来的方式：运行 scripts\uninstall_pg_service.ps1，再双击 EvoWiki.exe"
    }
    Say '7/8 启动' $how "$ServiceName，127.0.0.1:$port"
    Test-Database $bin $port $url
}

# ---------- 8. .env ----------

function Update-EnvPort([string]$envPath, [int]$port) {
    $bytes = [IO.File]::ReadAllBytes($envPath)
    $bom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
    $text = (New-Object System.Text.UTF8Encoding $false).GetString($bytes, $(if ($bom) { 3 } else { 0 }), $bytes.Length - $(if ($bom) { 3 } else { 0 }))
    $re = [regex]'(?m)^(\s*(?:export\s+)?EVOWIKI_DATABASE_URL\s*=\s*["'']?[a-z+]+://[^@/\r\n]*@?(?:127\.0\.0\.1|localhost):)(\d+)'
    $m = $re.Matches($text)
    if ($m.Count -ne 1) { Fail "$envPath 里 EVOWIKI_DATABASE_URL 不是一行 127.0.0.1:<端口> 形式（找到 $($m.Count) 处），没有改它；请手动把端口改成 $port" }
    $old = [int]$m[0].Groups[2].Value
    $others = @([regex]::Matches($text, '(?m)^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=') |
                ForEach-Object { $_.Groups[1].Value } | Where-Object { $_ -ne 'EVOWIKI_DATABASE_URL' })
    if ($old -eq $port) { Say '8/8 .env' '跳过' "EVOWIKI_DATABASE_URL 端口已是 $port"; return }
    $new = $re.Replace($text, ('${1}' + $port), 1)
    $out = $Utf8NoBom.GetBytes($new)
    if ($bom) { $out = [byte[]](@(0xEF, 0xBB, 0xBF) + $out) }
    [IO.File]::WriteAllBytes($envPath, $out)
    Say '8/8 .env' '已修改' ("EVOWIKI_DATABASE_URL 端口 {0} → {1}；其他 {2} 个键没动（{3}），值都没显示" -f
                             $old, $port, $others.Count, ($others -join ', '))
}

# ---------- main ----------

if ($EnvOnly) {
    if (-not $EnvFile) { Fail '-EnvOnly 需要 -EnvFile <文件>' }
    Update-EnvPort ([IO.Path]::GetFullPath($EnvFile)) $Port
    exit 0
}
if ($ConfigOnly -and $DataDir -and $LogDir) {
    Write-LoggingConf ([IO.Path]::GetFullPath($DataDir)) ([IO.Path]::GetFullPath($LogDir)) | Out-Null
    exit 0
}
if (-not $Main) { $Main = Resolve-Main }
$Main = [IO.Path]::GetFullPath($Main)
if (-not $DataDir) { $DataDir = Join-Path $Main '.pgdata' }
if (-not $BinRoot) { $BinRoot = Join-Path $Main '.pg18' }
if (-not $LogDir) { $LogDir = Join-Path $Main '.pglog' }
$DataDir = [IO.Path]::GetFullPath($DataDir); $BinRoot = [IO.Path]::GetFullPath($BinRoot); $LogDir = [IO.Path]::GetFullPath($LogDir)
$Source = Join-Path $Main '.venv\Lib\site-packages\pixeltable_pgserver\pginstall18'
$Bin = Join-Path $BinRoot 'bin'
$EnvPath = Join-Path $Main '.env'

if ($CopyOnly) { Copy-PgBinaries $Source $BinRoot; exit 0 }
if ($ConfigOnly) { Write-LoggingConf $DataDir $LogDir | Out-Null; exit 0 }

if (-not (Test-Admin)) { Fail '需要管理员权限：右键 PowerShell →“以管理员身份运行”，再运行本脚本' }
if (-not (Test-Path (Join-Path $DataDir 'PG_VERSION'))) { Fail "$DataDir 不是 Postgres 数据目录（没有 PG_VERSION）" }
$pgv = (Get-Content (Join-Path $DataDir 'PG_VERSION') -TotalCount 1).Trim()
if ($pgv -ne '18') { Fail "$DataDir 是 Postgres $pgv 的数据目录，这里的二进制是 18" }
$url = Get-EnvUrl $EnvPath

Copy-PgBinaries $Source $BinRoot
$confChanged = Write-LoggingConf $DataDir $LogDir
$row = Get-ServiceRow $ServiceName
if ($row -and $row.State -eq 'Running' -and (Test-ServiceMatches $row (Join-Path $Bin 'pg_ctl.exe') $DataDir $Port $Account)) {
    Say '3/8 端口' '跳过' "$ServiceName 已在 $Port 运行"
} else {
    Test-PortUsable $Port
}
Register-PgService $Bin $DataDir $Port
Stop-OldServer $Bin $DataDir
$sid = Get-Sid $Account
Grant-Acl $DataDir $sid 'M'
Grant-Acl $LogDir $sid 'M'
Grant-Acl $BinRoot $sid 'RX'
Say '6/8 权限' 'OK' "$Account：$DataDir、$LogDir 修改；$BinRoot 读取和执行"
Start-PgService $Bin $Port $url $confChanged
Update-EnvPort $EnvPath $Port

Write-Host ''
Write-Host "完成：开发库现在是 Windows 服务 $ServiceName（自动启动，127.0.0.1:$Port，日志 $LogDir）。" -ForegroundColor Green
Write-Host '接下来（普通 PowerShell 即可，不需要管理员）：'
Write-Host "  1. 重启后端，让它读到新端口：在托盘菜单选择 Restart service（重启服务）"
Write-Host '     其他会话自己启动的后端（例如 8002 上的）也要各自重启；写死旧端口的地方要改成新端口。'
Write-Host "  2. 查看状态：Get-Service $ServiceName；uv run python scripts\evowiki_ctl.py status"
Write-Host "  3. 撤销：以管理员身份运行 scripts\uninstall_pg_service.ps1"
