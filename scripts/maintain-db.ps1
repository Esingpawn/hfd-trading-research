param(
  [int]$Port = 8000,
  [switch]$Checkpoint,
  [switch]$Optimize,
  [switch]$Indexes
)

$ErrorActionPreference = "Stop"
$base = "http://127.0.0.1:$Port"

function Invoke-JsonUtf8 {
  param(
    [string]$Url,
    [string]$Method = "GET",
    [int]$TimeoutSec = 120
  )
  $params = @{ Uri = $Url; Method = $Method; TimeoutSec = $TimeoutSec }
  return Invoke-RestMethod @params
}

try {
  Invoke-JsonUtf8 "$base/health" | Out-Null
} catch {
  Write-Host "Cannot connect to HFD system: $($_.Exception.Message)"
  Write-Host "Start it first: .\scripts\start-system.ps1 -Port $Port"
  exit 1
}

if (-not $Checkpoint -and -not $Optimize -and -not $Indexes) {
  Write-Host "No maintenance action selected. Showing storage status only. Optional switches: -Indexes -Checkpoint -Optimize"
}

if ($Indexes) {
  Write-Host "Ensuring performance indexes..."
  $result = Invoke-JsonUtf8 "$base/system/storage/indexes" "POST" 300
  Write-Host "Index check completed: $($result.indexes.Count) required indexes"
}

if ($Checkpoint) {
  Write-Host "Running WAL checkpoint(TRUNCATE)..."
  $result = Invoke-JsonUtf8 "$base/system/storage/checkpoint?truncate=true" "POST" 300
  Write-Host "Checkpoint completed: busy=$($result.busy), log_frames=$($result.log_frames), checkpointed=$($result.checkpointed_frames)"
}

if ($Optimize) {
  Write-Host "Running PRAGMA optimize..."
  $result = Invoke-JsonUtf8 "$base/system/storage/optimize" "POST" 300
  Write-Host "Optimize completed: $($result.status)"
}

$storage = Invoke-JsonUtf8 "$base/system/storage" "GET" 120
Write-Host ""
Write-Host "Database storage status"
Write-Host "Kind: $($storage.database_url_kind)"
Write-Host "SQLite total: $($storage.sqlite.total_gb) GB"
foreach ($file in $storage.sqlite.files) {
  Write-Host "- $($file.path): $($file.mb) MB"
}
Write-Host "Raw payload: $($storage.raw_payload.raw_gb) GB, average $($storage.raw_payload.avg_raw_kb) KB/snapshot"
Write-Host "Missing performance indexes: $($storage.indexes.missing_required.Count)"
if ($storage.recommendations.Count -gt 0) {
  Write-Host "Recommendations:"
  foreach ($item in $storage.recommendations) { Write-Host "- $item" }
}
