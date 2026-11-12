<#
.SYNOPSIS
    一键拉起 WeKnora 本地扩展服务（非容器）。
.DESCRIPTION
    管理以下 4 个本地 Python 服务（Docker 容器不在此脚本范围内）：
      review     review-workbench/app.py          端口 8770  人工校对台
      validator  citation-validator/server.py     端口 8780  出处校验边车
      mcp        cert-extractor/server.py         端口 8765  MCP 证书抽取
      fixer      ingest-fixer/fixer.py --watch    无端口      摄入修正轮询
    日志写入 logs\<name>.out.log / .err.log，PID 写入 .run\<name>.pid。
.PARAMETER Action
    start / stop / restart / status，默认 start。
.PARAMETER Only
    只操作指定服务，逗号分隔，例如 -Only mcp,validator。
.PARAMETER Force
    服务已在运行时先停止再重新启动。
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\local-services.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\local-services.ps1 -Action status
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\local-services.ps1 -Action restart -Only mcp,validator
#>
[CmdletBinding()]
param(
    [ValidateSet('start', 'stop', 'restart', 'status')]
    [string]$Action = 'start',
    [string[]]$Only,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$Root    = Split-Path -Parent $PSScriptRoot
$Python  = Join-Path $Root '.venv\Scripts\python.exe'
$LogDir  = Join-Path $Root 'logs'
$RunDir  = Join-Path $Root '.run'
$WaitSec = 30

$Services = @(
    [pscustomobject]@{ Name = 'review';    Dir = 'review-workbench';   Script = 'app.py';    Args = @();                      Port = 8770; Desc = '人工校对台' }
    [pscustomobject]@{ Name = 'validator'; Dir = 'citation-validator'; Script = 'server.py'; Args = @();                      Port = 8780; Desc = '出处校验边车' }
    [pscustomobject]@{ Name = 'mcp';       Dir = 'cert-extractor';     Script = 'server.py'; Args = @();                      Port = 8765; Desc = 'MCP 证书抽取' }
    [pscustomobject]@{ Name = 'fixer';     Dir = 'ingest-fixer';       Script = 'fixer.py';  Args = @('--watch', '--interval', '30'); Port = 0; Desc = '摄入修正轮询' }
)

function Get-Targets {
    if (-not $Only) { return $Services }
    $unknown = $Only | Where-Object { $Services.Name -notcontains $_ }
    if ($unknown) {
        throw ("未知服务: {0}。可选值: {1}" -f ($unknown -join ','), (($Services.Name) -join ','))
    }
    return $Services | Where-Object { $Only -contains $_.Name }
}

function Get-PortOwner([int]$Port) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($conn) { return [int]$conn.OwningProcess }
    return $null
}

function Get-ChildProcessIds([int]$ParentId) {
    $ids = @()
    foreach ($child in (Get-CimInstance Win32_Process -Filter "ParentProcessId=$ParentId" -ErrorAction SilentlyContinue)) {
        $ids += [int]$child.ProcessId
        $ids += Get-ChildProcessIds ([int]$child.ProcessId)
    }
    return $ids
}

function Stop-ProcessTree([int]$ProcessId) {
    if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { return }
    foreach ($childId in (Get-ChildProcessIds $ProcessId)) {
        Stop-Process -Id $childId -Force -ErrorAction SilentlyContinue
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

function Get-ServicePid($Svc) {
    if ($Svc.Port -gt 0) {
        $owner = Get-PortOwner $Svc.Port
        if ($owner) { return $owner }
    }
    $pidFile = Join-Path $RunDir ($Svc.Name + '.pid')
    if (Test-Path $pidFile) {
        $procId = 0
        if ([int]::TryParse((Get-Content $pidFile -Raw).Trim(), [ref]$procId)) {
            $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
            if ($proc -and $proc.ProcessName -like 'python*') { return $procId }
        }
    }
    return $null
}

function Test-Dependencies {
    $deps = @(
        [pscustomobject]@{ Name = 'WeKnora API'; Url = 'http://127.0.0.1:8080/health'; Hint = '启动 WeKnora 容器（Docker Desktop）' }
        [pscustomobject]@{ Name = 'Ollama';      Url = 'http://127.0.0.1:11434/api/tags'; Hint = '启动 Ollama 客户端' }
        [pscustomobject]@{ Name = 'TEI Rerank';  Url = 'http://127.0.0.1:8081/info';      Hint = 'docker start tei-rerank' }
    )
    foreach ($dep in $deps) {
        try {
            Invoke-WebRequest -Uri $dep.Url -UseBasicParsing -TimeoutSec 3 | Out-Null
        }
        catch {
            Write-Host ("[warn] 依赖不可用: {0}  {1}  提示: {2}" -f $dep.Name, $dep.Url, $dep.Hint) -ForegroundColor Yellow
        }
    }
}

function Start-ServiceItem($Svc) {
    $running = Get-ServicePid $Svc
    if ($running) {
        if (-not $Force) {
            Write-Host ("[skip] {0} 已在运行 (PID {1})" -f $Svc.Name, $running) -ForegroundColor Yellow
            return
        }
        Stop-ServiceItem $Svc | Out-Null
    }

    $outLog = Join-Path $LogDir ($Svc.Name + '.out.log')
    $errLog = Join-Path $LogDir ($Svc.Name + '.err.log')
    Set-Content -Path $outLog -Value '' -Encoding UTF8
    Set-Content -Path $errLog -Value '' -Encoding UTF8

    $argList = @($Svc.Script) + $Svc.Args
    $proc = Start-Process -FilePath $Python `
        -ArgumentList $argList `
        -WorkingDirectory (Join-Path $Root $Svc.Dir) `
        -WindowStyle Hidden `
        -RedirectStandardOutput $outLog `
        -RedirectStandardError $errLog `
        -PassThru

    # venv 的 python.exe 可能是启动器，真正跑服务的是它的子进程；定位含脚本名的子进程
    Start-Sleep -Milliseconds 1200
    $actualId = $proc.Id
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($proc.Id)" -ErrorAction SilentlyContinue
    $serverChild = $children | Where-Object { $_.CommandLine -like ('*' + $Svc.Script + '*') } | Select-Object -First 1
    if ($serverChild) { $actualId = [int]$serverChild.ProcessId }

    Set-Content -Path (Join-Path $RunDir ($Svc.Name + '.pid')) -Value $actualId -Encoding ASCII
    Write-Host ("[start] {0,-9} PID {1,-6} {2}" -f $Svc.Name, $actualId, $Svc.Desc) -ForegroundColor Cyan
}

function Stop-ServiceItem($Svc) {
    $procId  = Get-ServicePid $Svc
    $pidFile = Join-Path $RunDir ($Svc.Name + '.pid')

    $storedId = 0
    if (Test-Path $pidFile) {
        [void][int]::TryParse((Get-Content $pidFile -Raw).Trim(), [ref]$storedId)
    }

    if (-not $procId -and -not (Get-Process -Id $storedId -ErrorAction SilentlyContinue)) {
        if (Test-Path $pidFile) { Remove-Item $pidFile -Force }
        Write-Host ("[stop]  {0,-9} 未在运行" -f $Svc.Name) -ForegroundColor DarkGray
        return
    }

    # 端口占用进程与 pid 文件记录的进程都按进程树终止，避免启动器残留
    $targetIds = @()
    foreach ($id in @($procId, $storedId)) {
        if ($id -and ($targetIds -notcontains $id)) { $targetIds += $id }
    }
    foreach ($id in $targetIds) { Stop-ProcessTree $id }
    Start-Sleep -Milliseconds 800
    foreach ($id in $targetIds) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
    if (Test-Path $pidFile) { Remove-Item $pidFile -Force }
    Write-Host ("[stop]  {0,-9} 已停止 (PID {1})" -f $Svc.Name, ($targetIds -join ',')) -ForegroundColor Green
}

function Wait-Ready($Svc) {
    $deadline = (Get-Date).AddSeconds($WaitSec)
    while ((Get-Date) -lt $deadline) {
        $procId = Get-ServicePid $Svc
        if (-not $procId) { return $false }
        if ($Svc.Port -gt 0) {
            if (Get-PortOwner $Svc.Port) { return $true }
        }
        else {
            # 无端口服务：进程存活即视为启动成功
            return [bool](Get-Process -Id $procId -ErrorAction SilentlyContinue)
        }
        Start-Sleep -Milliseconds 800
    }
    return $false
}

function Show-Status($Targets) {
    $rows = foreach ($svc in $Targets) {
        $procId = Get-ServicePid $svc
        [pscustomobject]@{
            服务   = $svc.Name
            说明   = $svc.Desc
            端口   = if ($svc.Port -gt 0) { $svc.Port } else { '-' }
            状态   = if ($procId) { 'RUNNING' } else { 'DOWN' }
            PID    = if ($procId) { $procId } else { '-' }
            日志   = (Join-Path $LogDir ($svc.Name + '.out.log'))
        }
    }
    $rows | Format-Table -AutoSize
}

function Show-FailureHint($Svc) {
    Write-Host ("[fail] {0} 未就绪，最近日志：" -f $Svc.Name) -ForegroundColor Red
    foreach ($file in @('err', 'out')) {
        $path = Join-Path $LogDir ($Svc.Name + '.' + $file + '.log')
        if ((Test-Path $path) -and (Get-Item $path).Length -gt 0) {
            Write-Host ("  --- {0} ---" -f $file) -ForegroundColor DarkYellow
            Get-Content $path -Tail 15 | ForEach-Object { Write-Host ("  " + $_) }
        }
    }
}

# ---------------- 主流程 ----------------

if (-not (Test-Path $Python)) {
    throw ("未找到虚拟环境 Python: {0}。请先创建 .venv（py -3.11 -m venv .venv）。" -f $Python)
}
foreach ($dir in @($LogDir, $RunDir)) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
}

$targets = @(Get-Targets)

switch ($Action) {
    'stop' {
        foreach ($svc in ($targets | Sort-Object Port)) { Stop-ServiceItem $svc }
    }
    'start' {
        if (-not $Only) { Test-Dependencies }
        foreach ($svc in $targets) { Start-ServiceItem $svc }
        $failed = @()
        foreach ($svc in $targets) {
            if (Wait-Ready $svc) {
                Write-Host ("[ready] {0,-9} {1}" -f $svc.Name, $(if ($svc.Port -gt 0) { "http://127.0.0.1:$($svc.Port)" } else { '轮询中' })) -ForegroundColor Green
            }
            else {
                $failed += $svc
                Show-FailureHint $svc
            }
        }
        ""
        Show-Status $targets
        if ($failed.Count -gt 0) {
            Write-Host ("启动失败: {0}" -f (($failed | ForEach-Object { $_.Name }) -join ',')) -ForegroundColor Red
            exit 1
        }
        Write-Host "入口: 前端 http://localhost  |  API http://127.0.0.1:8080  |  校对台 http://127.0.0.1:8770/api/reviews" -ForegroundColor DarkCyan
    }
    'restart' {
        foreach ($svc in ($targets | Sort-Object Port)) { Stop-ServiceItem $svc }
        if (-not $Only) { Test-Dependencies }
        foreach ($svc in $targets) { Start-ServiceItem $svc }
        $failed = @()
        foreach ($svc in $targets) {
            if (-not (Wait-Ready $svc)) { $failed += $svc; Show-FailureHint $svc }
        }
        ""
        Show-Status $targets
        if ($failed.Count -gt 0) { exit 1 }
    }
    'status' {
        Show-Status $targets
    }
}

exit 0
