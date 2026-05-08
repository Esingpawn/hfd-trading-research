param(
  [int]$Port = 8000,
  [int]$CollectIntervalSeconds = 1800,
  [int]$ResearchShortIntervalSeconds = 1800,
  [int]$ResearchMidIntervalSeconds = 3600,
  [int]$ResearchLongIntervalSeconds = 14400,
  [int]$PaperIntervalSeconds = 60,
  [string[]]$Coins = @("BTC", "ETH", "SOL", "BNB", "LINK", "TON", "DOGE", "HYPE", "ZEC")
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$RuntimeDir = Join-Path $Root "data\runtime"
$LogDir = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $RuntimeDir, $LogDir | Out-Null

function Test-PidRunning {
  param([int]$PidValue)
  if ($PidValue -le 0) { return $false }
  return [bool](Get-Process -Id $PidValue -ErrorAction SilentlyContinue)
}

function Get-ListeningPid {
  param([int]$LocalPort)
  try {
    $conn = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction Stop | Select-Object -First 1
    if ($conn) { return [int]$conn.OwningProcess }
  } catch {
    return $null
  }
  return $null
}

function Get-CollectLoopPid {
  try {
    $proc = Get-CimInstance Win32_Process -ErrorAction Stop |
      Where-Object { $_.CommandLine -match "app\.cli" -and ($_.CommandLine -match "collect-tiered-loop" -or $_.CommandLine -match "collect-loop") } |
      Select-Object -First 1
    if ($proc) { return [int]$proc.ProcessId }
  } catch {
    return $null
  }
  return $null
}

function Get-PaperLoopPid {
  try {
    $proc = Get-CimInstance Win32_Process -ErrorAction Stop |
      Where-Object { $_.CommandLine -match "app\.cli" -and $_.CommandLine -match "paper-loop" } |
      Select-Object -First 1
    if ($proc) { return [int]$proc.ProcessId }
  } catch {
    return $null
  }
  return $null
}

function Write-ProcessMeta {
  param(
    [string]$Name,
    [int]$PidValue,
    [string]$Command,
    [string]$StdoutLog,
    [string]$StderrLog,
    [hashtable]$Extra = @{}
  )
  $meta = @{
    name = $Name
    pid = $PidValue
    command = $Command
    started_at = (Get-Date).ToUniversalTime().ToString("o")
    stdout_log = $StdoutLog
    stderr_log = $StderrLog
  }
  foreach ($key in $Extra.Keys) { $meta[$key] = $Extra[$key] }
  Set-Content -LiteralPath (Join-Path $RuntimeDir "$Name.pid") -Value $PidValue -Encoding utf8
  $meta | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $RuntimeDir "$Name.json") -Encoding utf8
}

function Wait-Health {
  param([int]$LocalPort)
  for ($i = 0; $i -lt 20; $i++) {
    try {
      $health = Invoke-RestMethod "http://127.0.0.1:$LocalPort/health" -TimeoutSec 2
      if ($health.status -eq "ok") { return $true }
    } catch {
      Start-Sleep -Milliseconds 500
    }
  }
  return $false
}

Set-Location $Root
python -m app.cli init-db | Out-Null

$serverPid = Get-ListeningPid -LocalPort $Port
$serverOut = Join-Path $LogDir "server.out.log"
$serverErr = Join-Path $LogDir "server.err.log"
if ($serverPid -and (Test-PidRunning -PidValue $serverPid)) {
  Write-Host "FastAPI 已在端口 $Port 运行，PID: $serverPid"
  Write-ProcessMeta -Name "server" -PidValue $serverPid -Command "existing listener on $Port" -StdoutLog $serverOut -StderrLog $serverErr
} else {
  $serverArgs = @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$Port")
  $server = Start-Process -FilePath "python" -ArgumentList $serverArgs -WorkingDirectory $Root -RedirectStandardOutput $serverOut -RedirectStandardError $serverErr -WindowStyle Hidden -PassThru
  Write-ProcessMeta -Name "server" -PidValue $server.Id -Command "python $($serverArgs -join ' ')" -StdoutLog $serverOut -StderrLog $serverErr
  Write-Host "FastAPI 已启动，PID: $($server.Id)"
}

if (-not (Wait-Health -LocalPort $Port)) {
  throw "FastAPI 健康检查失败，请查看 $serverErr"
}

try {
  $base = "http://127.0.0.1:$Port"
  Invoke-RestMethod "$base/data/completeness" -TimeoutSec 120 | Out-Null
  Invoke-RestMethod "$base/market/overview" -TimeoutSec 180 | Out-Null
} catch {
  Write-Host "缓存预热未完成：$($_.Exception.Message)"
}

$collectorPidFile = Join-Path $RuntimeDir "collect-core-loop.pid"
$collectorPid = $null
if (Test-Path $collectorPidFile) {
  $rawPid = (Get-Content -LiteralPath $collectorPidFile -Raw).Trim()
  if ($rawPid -match "^\d+$") { $collectorPid = [int]$rawPid }
}
if (-not ($collectorPid -and (Test-PidRunning -PidValue $collectorPid))) {
  $collectorPid = Get-CollectLoopPid
}

$collectorOut = Join-Path $LogDir "collect-core-loop.out.log"
$collectorErr = Join-Path $LogDir "collect-core-loop.err.log"
$coreIndicators = @("smart_money_cost", "liq_heatmap", "cross_exchange_resonance", "imbalance", "trend_exhaustion")
$researchIndicators = @("trend_price", "inst_vwap", "liquidation_fuel", "liquidity_sweep", "inst_volume_profile", "hvn_nodes", "micro_poc")
if ($collectorPid -and (Test-PidRunning -PidValue $collectorPid)) {
  Write-Host "分层采集循环已在运行，PID: $collectorPid"
} else {
  $collectorArgs = @(
    "-m", "app.cli", "collect-tiered-loop",
    "--coins"
  ) + $Coins + @(
    "--timeframes", "short", "mid", "long",
    "--core-interval-seconds", "$CollectIntervalSeconds",
    "--research-short-interval-seconds", "$ResearchShortIntervalSeconds",
    "--research-mid-interval-seconds", "$ResearchMidIntervalSeconds",
    "--research-long-interval-seconds", "$ResearchLongIntervalSeconds"
  )
  $collector = Start-Process -FilePath "python" -ArgumentList $collectorArgs -WorkingDirectory $Root -RedirectStandardOutput $collectorOut -RedirectStandardError $collectorErr -WindowStyle Hidden -PassThru
  $collectorPid = $collector.Id
  Write-Host "分层采集循环已启动，PID: $collectorPid"
}

Write-ProcessMeta -Name "collect-core-loop" -PidValue $collectorPid -Command "python -m app.cli collect-tiered-loop" -StdoutLog $collectorOut -StderrLog $collectorErr -Extra @{
  interval_seconds = $CollectIntervalSeconds
  mode = "tiered"
  coins = $Coins
  timeframes = @("short", "mid", "long")
  indicators = $coreIndicators
  research_indicators = $researchIndicators
  research_intervals = @{
    short = $ResearchShortIntervalSeconds
    mid = $ResearchMidIntervalSeconds
    long = $ResearchLongIntervalSeconds
  }
}

$paperPidFile = Join-Path $RuntimeDir "paper-loop.pid"
$paperPid = $null
if (Test-Path $paperPidFile) {
  $rawPid = (Get-Content -LiteralPath $paperPidFile -Raw).Trim()
  if ($rawPid -match "^\d+$") { $paperPid = [int]$rawPid }
}
if (-not ($paperPid -and (Test-PidRunning -PidValue $paperPid))) {
  $paperPid = Get-PaperLoopPid
}

$paperOut = Join-Path $LogDir "paper-loop.out.log"
$paperErr = Join-Path $LogDir "paper-loop.err.log"
if ($paperPid -and (Test-PidRunning -PidValue $paperPid)) {
  Write-Host "纸上交易循环已在运行，PID: $paperPid"
} else {
  $paperArgs = @(
    "-m", "app.cli", "paper-loop",
    "--coins"
  ) + $Coins + @(
    "--notify",
    "--interval-seconds", "$PaperIntervalSeconds"
  )
  $paper = Start-Process -FilePath "python" -ArgumentList $paperArgs -WorkingDirectory $Root -RedirectStandardOutput $paperOut -RedirectStandardError $paperErr -WindowStyle Hidden -PassThru
  $paperPid = $paper.Id
  Write-Host "纸上交易循环已启动，PID: $paperPid"
}

Write-ProcessMeta -Name "paper-loop" -PidValue $paperPid -Command "python -m app.cli paper-loop" -StdoutLog $paperOut -StderrLog $paperErr -Extra @{
  interval_seconds = $PaperIntervalSeconds
  coins = $Coins
  notify = $true
  trigger = "new_completed_collection_run"
}

Write-Host ""
Write-Host "HFD 系统已就绪"
Write-Host "面板: http://127.0.0.1:$Port/dashboard"
Write-Host "状态: http://127.0.0.1:$Port/system/runtime"


