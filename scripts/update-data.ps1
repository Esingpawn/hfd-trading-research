param(
  [int]$Port = 8000,
  [string[]]$Coins = @("BTC", "ETH", "SOL", "BNB", "LINK", "TON", "DOGE", "HYPE", "ZEC")
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Test-Health {
  param([int]$LocalPort)
  try {
    $health = Invoke-RestMethod "http://127.0.0.1:$LocalPort/health" -TimeoutSec 2
    return $health.status -eq "ok"
  } catch {
    return $false
  }
}

function New-CoinQuery {
  param([string[]]$CoinList)
  return ($CoinList | ForEach-Object { "coins=$([uri]::EscapeDataString($_.ToUpper()))" }) -join "&"
}

if (-not (Test-Health -LocalPort $Port)) {
  Write-Host "服务未运行，先启动系统..."
  & (Join-Path $Root "scripts\start-system.ps1") -Port $Port -Coins $Coins
}

$base = "http://127.0.0.1:$Port"
$coinQuery = New-CoinQuery -CoinList $Coins
$timeframeQuery = "timeframes=short&timeframes=mid&timeframes=long"
$researchIndicators = @(
  "trend_price",
  "inst_vwap",
  "liquidation_fuel",
  "liquidity_sweep",
  "inst_volume_profile",
  "hvn_nodes",
  "micro_poc"
)
$indicatorQuery = ($researchIndicators | ForEach-Object { "indicators=$([uri]::EscapeDataString($_))" }) -join "&"

Write-Host "正在补齐评分核心数据..."
$core = Invoke-RestMethod -Method Post "$base/collect/scoring-core?dry_run=false&$coinQuery&$timeframeQuery" -TimeoutSec 600
Write-Host "核心数据完成：snapshots=$($core.snapshots_written), prices=$($core.prices_written), errors=$($core.errors.Count)"

Write-Host "正在补齐全量研究数据..."
$full = Invoke-RestMethod -Method Post "$base/collect/run?dry_run=false&$coinQuery&$timeframeQuery&$indicatorQuery" -TimeoutSec 900
Write-Host "全量研究完成：snapshots=$($full.snapshots_written), prices=$($full.prices_written), errors=$($full.errors.Count)"

$summary = Invoke-RestMethod "$base/data/completeness" -TimeoutSec 120
Invoke-RestMethod "$base/market/overview" -TimeoutSec 180 | Out-Null
$scoring = $summary.summary.scoring
$research = $summary.summary.research
Write-Host ""
Write-Host "数据覆盖已更新"
Write-Host ("评分核心: 历史 {0:P1}, 新鲜 {1:P1}, 缺失 {2}, 过期 {3}" -f $scoring.coverage_pct, $scoring.fresh_coverage_pct, $scoring.missing_slots, $scoring.stale_slots)
Write-Host ("全量研究: 历史 {0:P1}, 新鲜 {1:P1}, 缺失 {2}, 过期 {3}" -f $research.coverage_pct, $research.fresh_coverage_pct, $research.missing_slots, $research.stale_slots)
Write-Host "面板: $base/dashboard"


