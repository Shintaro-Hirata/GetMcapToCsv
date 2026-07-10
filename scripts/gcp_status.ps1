# scripts/gcp_status.ps1
# 「今、課金対象のリソースが残っていないか」を確認する (読み取りのみ・安全)。
# モデルB (毎回作って消す) で、削除し忘れ・クラッシュで VM が残っていないかの点検用。
#
# 使い方 (PowerShell):
#   .\scripts\gcp_status.ps1
# 何も表示されなければ、この用途での課金はありません。

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Read-GcpEnv([string]$path) {
  if (-not (Test-Path $path)) { throw "$path がありません。" }
  $h = @{}
  foreach ($line in Get-Content -LiteralPath $path) {
    if ($line -match '^\s*#') { continue }
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
      $k = $matches[1]; $v = $matches[2]
      if ($v -match '^\s*"([^"]*)"') { $v = $matches[1] }
      elseif ($v -match "^\s*'([^']*)'") { $v = $matches[1] }
      else { $v = ($v -split '#', 2)[0].Trim() }
      $h[$k] = $v
    }
  }
  return $h
}

$envFile = if ($env:GCP_ENV) { $env:GCP_ENV } else { Join-Path $scriptDir 'gcp.env' }
$cfg = Read-GcpEnv $envFile
$project = $cfg['GCP_PROJECT']
$vm = $cfg['GCP_VM']

Write-Host "=== 課金対象リソースの点検 (project: $project) ==="
Write-Host ''
Write-Host "--- VM インスタンス (name=$vm) ---  ※ STATUS が RUNNING なら計算課金中"
& gcloud compute instances list --project=$project --filter="name=$vm" --format='table(name,zone,status)'
Write-Host ''
Write-Host "--- ディスク (name=$vm) ---  ※ 存在するだけで (停止中でも) 課金"
& gcloud compute disks list --project=$project --filter="name=$vm" --format='table(name,zone,sizeGb,status)'
Write-Host ''
Write-Host "--- 予約済み外部IP ---  ※ 未使用でも課金 (通常は無いはず)"
& gcloud compute addresses list --project=$project --format='table(name,region,address,status)'
Write-Host ''
Write-Host "上に $vm の行が無ければ、この用途での課金はありません。"
Write-Host "残っていた場合の削除:"
Write-Host "  gcloud compute instances delete $vm --project=$project --zone=<ZONE>"
