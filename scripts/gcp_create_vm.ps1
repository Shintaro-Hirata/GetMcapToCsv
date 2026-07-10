# scripts/gcp_create_vm.ps1
# PowerShell VM-creation script (ASCII only, so Windows PowerShell 5.1 reads it
# correctly regardless of console codepage). Creates one small VM in the bucket's
# region for mcap->CSV conversion. Reads scripts/gcp.env.
#
# Usage:
#   Copy-Item scripts\gcp.env.example scripts\gcp.env   # then edit
#   .\scripts\gcp_create_vm.ps1
param(
  [switch]$Yes   # skip the confirmation prompt (for UI / automation)
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Read-GcpEnv([string]$path) {
  if (-not (Test-Path $path)) {
    throw "$path not found. Copy scripts\gcp.env.example to scripts\gcp.env and edit it."
  }
  $h = @{}
  foreach ($line in Get-Content -LiteralPath $path) {
    if ($line -match '^\s*#') { continue }
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
      $k = $matches[1]; $v = $matches[2]
      # quoted value -> take inside quotes (ignore trailing comment); unquoted -> strip from '#'
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

function Cfg([string]$key, $default = $null) {
  if ($cfg.ContainsKey($key) -and $cfg[$key] -ne '') { return $cfg[$key] } else { return $default }
}

$project = Cfg 'GCP_PROJECT'; if (-not $project) { throw 'Set GCP_PROJECT in gcp.env' }
$zone    = Cfg 'GCP_ZONE';    if (-not $zone)    { throw 'Set GCP_ZONE in gcp.env' }
$vm      = Cfg 'GCP_VM';      if (-not $vm)      { throw 'Set GCP_VM in gcp.env' }

if ($project -eq 'your-project-id' -or $vm -eq 'your-vm-name') {
  throw @"
gcp.env is not edited (GCP_PROJECT / GCP_VM are still the samples). Set real values.
  current GCP_PROJECT = '$project'
List usable projects:  gcloud projects list
Default project     :  gcloud config get-value project
"@
}

$machineType = Cfg 'MACHINE_TYPE' 'e2-standard-4'
$bootDiskGb  = Cfg 'BOOT_DISK_GB' '30'   # OS + temp is plenty; keeps stopped-disk cost low (30GB ~= 450 JPY/mo)
$imageFamily = Cfg 'IMAGE_FAMILY' 'debian-12'
$imageProj   = Cfg 'IMAGE_PROJECT' 'debian-cloud'
$private     = (Cfg 'PRIVATE' '0') -eq '1'
$iapTag      = Cfg 'IAP_TAG' 'mcap-iap-ssh'

$sshFlags = @()
if ($cfg.ContainsKey('GCLOUD_SSH_FLAGS') -and $cfg['GCLOUD_SSH_FLAGS']) {
  $sshFlags = $cfg['GCLOUD_SSH_FLAGS'] -split '\s+'
}
$netArgs = @()
if ($private) {
  $netArgs = @('--no-address', "--tags=$iapTag", '--metadata=enable-oslogin=TRUE',
               '--shielded-secure-boot', '--shielded-vtpm', '--shielded-integrity-monitoring')
  if ($sshFlags -notcontains '--tunnel-through-iap') { $sshFlags += '--tunnel-through-iap' }
}

Write-Host '[info] Creating VM:'
Write-Host "  project : $project"
Write-Host "  zone    : $zone"
Write-Host "  name    : $vm"
Write-Host "  machine : $machineType / boot disk ${bootDiskGb}GB / $imageFamily"
Write-Host '  scope   : bucket read (devstorage.read_only)'
if ($private) {
  Write-Host '  network : private (no external IP / IAP SSH / OS Login)'
} else {
  Write-Host '  network : standard (external IP / SSH protected by IAM)'
}
if (-not $Yes) {
  $ans = Read-Host 'Create? [y/N]'
  if ($ans -ne 'y' -and $ans -ne 'Y') { Write-Host 'Aborted.'; exit 0 }
}

if ($private) {
  $fwRule = Cfg 'FW_RULE' "allow-iap-ssh-$iapTag"
  & gcloud compute firewall-rules describe $fwRule --project=$project 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Write-Host "[info] Creating IAP SSH firewall rule: $fwRule"
    & gcloud compute firewall-rules create $fwRule --project=$project `
      --direction=INGRESS --action=ALLOW --rules=tcp:22 `
      --source-ranges=35.235.240.0/20 --target-tags=$iapTag
    if ($LASTEXITCODE -ne 0) {
      Write-Warning 'Firewall create failed (permission/org policy). Ask an admin to allow tcp:22 from 35.235.240.0/20.'
    }
  }
}

$createArgs = @('compute', 'instances', 'create', $vm,
  "--project=$project", "--zone=$zone", "--machine-type=$machineType",
  "--image-family=$imageFamily", "--image-project=$imageProj",
  "--boot-disk-size=${bootDiskGb}GB", '--boot-disk-type=pd-balanced',
  '--scopes=https://www.googleapis.com/auth/devstorage.read_only') + $netArgs
& gcloud @createArgs
if ($LASTEXITCODE -ne 0) { throw 'VM creation failed. Check the error above.' }

Write-Host ''
Write-Host '[ok] Created. Installing Python (first boot)...'
$installCmd = 'sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv python3-pip >/dev/null && python3 --version'
$sshArgs = @('compute', 'ssh', $vm, "--project=$project", "--zone=$zone") + $sshFlags + @('--command', $installCmd)
& gcloud @sshArgs

Write-Host ''
if ($private) {
  Write-Host '[info] This VM is private (no external IP + IAP SSH). Put this in gcp.env:'
  Write-Host '         GCLOUD_SSH_FLAGS="--tunnel-through-iap"'
}
Write-Host '[ok] Ready. Extract with:'
Write-Host '  .\scripts\gcp_fetch.ps1 -Vehicle GIGA09 -Start "2026-07-01 20:40" -End "2026-07-01 20:45" -Topics topics.example.t2.json'
Write-Host ''
Write-Host 'When done, stop the VM to reduce cost:'
Write-Host "  gcloud compute instances stop $vm --zone $zone --project $project"
