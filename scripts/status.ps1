param([int]$Port = 8000)

$ErrorActionPreference = "Stop"
$base = "http://127.0.0.1:$Port"

function Invoke-JsonUtf8 {
  param(
    [string]$Url,
    [int]$TimeoutSec = 10
  )
  $request = [System.Net.WebRequest]::Create($Url)
  $request.Timeout = $TimeoutSec * 1000
  $response = $null
  $reader = $null
  try {
    $response = $request.GetResponse()
    $reader = New-Object System.IO.StreamReader($response.GetResponseStream(), [System.Text.Encoding]::UTF8)
    $text = $reader.ReadToEnd()
    return $text | ConvertFrom-Json
  } finally {
    if ($reader) { $reader.Close() }
    if ($response) { $response.Close() }
  }
}

try {
  $runtime = Invoke-JsonUtf8 "$base/system/runtime" 10
  $coverage = Invoke-JsonUtf8 "$base/data/completeness" 120
} catch {
  Write-Host "无法连接 HFD 系统：$($_.Exception.Message)"
  Write-Host "可以运行：.\scripts\start-system.ps1"
  exit 1
}

try {
  $telegram = Invoke-JsonUtf8 "$base/telegram/status" 8
} catch {
  $telegram = [pscustomobject]@{
    configured = $false
    has_chat_id = $false
    bot_username = $null
    error = $_.Exception.Message
  }
}

$scoring = $coverage.summary.scoring
$research = $coverage.summary.research
$diagnostics = $runtime.diagnostics

Write-Host "HFD 系统状态"
if ($diagnostics) {
  Write-Host "诊断: $($diagnostics.label) - $($diagnostics.summary)"
}
Write-Host "服务: $($runtime.server.running) PID=$($runtime.server.pid)"
Write-Host "分层采集: $($runtime.collector.running) PID=$($runtime.collector.pid)"
if ($runtime.paper_loop) {
  Write-Host "纸上循环: $($runtime.paper_loop.running) PID=$($runtime.paper_loop.pid)"
}
Write-Host "最近采集: $($runtime.collection.latest.finished_at)"
Write-Host "下次采集: $($runtime.collection.next_collect_at)"
Write-Host ("评分核心: 历史 {0:P1}, 新鲜 {1:P1}, 缺失 {2}, 过期 {3}" -f $scoring.coverage_pct, $scoring.fresh_coverage_pct, $scoring.missing_slots, $scoring.stale_slots)
Write-Host ("全量研究: 历史 {0:P1}, 新鲜 {1:P1}, 缺失 {2}, 过期 {3}" -f $research.coverage_pct, $research.fresh_coverage_pct, $research.missing_slots, $research.stale_slots)
if ($diagnostics -and $diagnostics.issues.Count -gt 0) {
  Write-Host ""
  Write-Host "诊断问题:"
  foreach ($issue in $diagnostics.issues) {
    Write-Host "- [$($issue.severity)] $($issue.message)"
    if ($issue.action) { Write-Host "  建议: $($issue.action)" }
  }
}
if ($telegram.error) {
  Write-Host "Telegram: 检查失败，$($telegram.error)"
} else {
  Write-Host "Telegram: configured=$($telegram.configured), chat_id=$($telegram.has_chat_id), bot=@$($telegram.bot_username)"
}
Write-Host "面板: $base/dashboard"




