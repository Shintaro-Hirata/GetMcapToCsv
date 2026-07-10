# scripts/gcp_status.ps1
# Check whether any billable worker resource is left (read-only, safe). ASCII only.
# For Model B (create/delete each time): confirm no VM was left after a crash.
#   .\scripts\gcp_status.ps1
# If the VM name does not appear, there is no charge for this use.

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Read-GcpEnv([string]$path) {
  if (-not (Test-Path $path)) { throw "$path not found." }
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

Write-Host "=== Billable resource check (project: $project) ==="
Write-Host ''
Write-Host "--- VM instances (name=$vm) ---  (STATUS RUNNING = compute is billing)"
& gcloud compute instances list --project=$project --filter="name=$vm" --format='table(name,zone,status)'
Write-Host ''
Write-Host "--- Disks (name=$vm) ---  (billed whenever it exists, even stopped)"
& gcloud compute disks list --project=$project --filter="name=$vm" --format='table(name,zone,sizeGb,status)'
Write-Host ''
Write-Host "--- Reserved external IPs (name=$vm) ---  (this tool never reserves one; usually empty)"
& gcloud compute addresses list --project=$project --filter="name~$vm" --format='table(name,region,address,status)'
Write-Host ''
Write-Host "If none of the sections show a $vm row, there is no charge for this use."
Write-Host "(In a shared project other people's VMs/IPs also exist; they are unrelated. Do not touch.)"
Write-Host "To delete if left over:"
Write-Host "  gcloud compute instances delete $vm --project=$project --zone=<ZONE>"
