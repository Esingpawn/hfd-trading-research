param(
  [string]$ProductionRemote = "production",
  [string]$RemoteCwd = "/opt/hfd-git.tmp",
  [string]$SshTarget = "root@124.221.31.75",
  [int]$SshPort = 2222,
  [string]$SshKey = $env:HFD_PROD_SSH_KEY,
  [string[]]$Services = @("api", "collector-worker", "paper-worker", "experiment-worker", "darkflow-worker", "task-worker"),
  [string]$HealthUrl = "https://esing.ccwu.cc/health",
  [switch]$SkipBuild,
  [switch]$PushOrigin,
  [switch]$AllowDirty
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (-not $SshKey) {
  $fallbackKey = "C:\Users\18097\Downloads\124.221.31.75_id_ed25519"
  if (Test-Path -LiteralPath $fallbackKey) { $SshKey = $fallbackKey }
}

function Invoke-GitText {
  param([string[]]$GitArgs)
  $output = & git @GitArgs
  if ($LASTEXITCODE -ne 0) { throw "git $($GitArgs -join ' ') failed" }
  return (($output | Out-String).Trim())
}

function Invoke-Remote {
  param([string]$Command)
  $sshArgs = @("-p", "$SshPort", "-o", "StrictHostKeyChecking=no")
  if ($SshKey) { $sshArgs += @("-i", $SshKey) }
  $sshArgs += @($SshTarget, $Command)
  & ssh @sshArgs
  if ($LASTEXITCODE -ne 0) { throw "remote command failed: $Command" }
}

function Test-Health {
  param([string]$Url)
  for ($i = 0; $i -lt 24; $i++) {
    try {
      $health = Invoke-RestMethod -Uri $Url -TimeoutSec 5
      if ($health.status -eq "ok") { return $true }
    } catch {
      Start-Sleep -Seconds 2
    }
  }
  return $false
}

$branch = Invoke-GitText -GitArgs @("rev-parse", "--abbrev-ref", "HEAD")
if ($branch -ne "main") { throw "production deploy must run from main; current branch is $branch" }

if (-not $AllowDirty) {
  & git diff --quiet
  if ($LASTEXITCODE -ne 0) { throw "tracked working tree changes exist; commit or pass -AllowDirty" }
  & git diff --cached --quiet
  if ($LASTEXITCODE -ne 0) { throw "staged changes exist; commit or pass -AllowDirty" }
}

$commit = Invoke-GitText -GitArgs @("rev-parse", "--short", "HEAD")
$NormalizedServices = @()
foreach ($service in $Services) {
  foreach ($item in ($service -split ",")) {
    $name = $item.Trim()
    if ($name) { $NormalizedServices += $name }
  }
}
$NormalizedServices = @($NormalizedServices | Select-Object -Unique)
if ($NormalizedServices.Count -eq 0) { throw "at least one service must be provided" }
foreach ($service in $NormalizedServices) {
  if ($service -notmatch "^[A-Za-z0-9][A-Za-z0-9_-]*$") { throw "invalid service name: $service" }
}

Write-Host "Deploying $commit to $ProductionRemote ($RemoteCwd)"

if ($PushOrigin) {
  Write-Host "Pushing origin/main"
  & git push origin main
  if ($LASTEXITCODE -ne 0) { throw "git push origin main failed" }
}

Write-Host "Pushing production/main"
& git push $ProductionRemote main
if ($LASTEXITCODE -ne 0) { throw "git push $ProductionRemote main failed" }

$serviceText = ($NormalizedServices | ForEach-Object { $_ }) -join " "
$remoteCommands = @(
  "set -e",
  "cd $RemoteCwd",
  "test -f .env",
  "git rev-parse --short HEAD",
  "docker compose config --services >/dev/null"
)
if (-not $SkipBuild) { $remoteCommands += "docker compose build $serviceText" }
$remoteCommands += "docker compose up -d --no-deps $serviceText"
$remoteCommands += "docker compose ps $serviceText"

Invoke-Remote ($remoteCommands -join "; ")

if (-not (Test-Health -Url $HealthUrl)) {
  throw "health check failed: $HealthUrl"
}

Write-Host "Production deploy complete: $commit"
Write-Host "Health: $HealthUrl"
