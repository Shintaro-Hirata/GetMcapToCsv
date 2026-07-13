# scripts/gcp_fetch.ps1
# PowerShell driver script (ASCII only, so Windows PowerShell 5.1 parses it
# correctly regardless of console codepage).
#   1. push the tool to the VM (incl. the private mcap-ros2idl-support package)
#   2. run mcap->CSV on the VM (GCS read inside GCP = egress-free)
#   3. download only the small CSV back
#
# Prereq: gcloud CLI installed and authenticated. scripts\gcp.env prepared.
# Usage:
#   .\scripts\gcp_fetch.ps1 -Vehicle GIGA09 -Start "2026-07-01 20:40" -End "2026-07-01 20:45" -Topics topics.example.t2.json
#   .\scripts\gcp_fetch.ps1 -Vehicle GIGA09 -Start "..." -End "..." -AllTopics -IncludeSensor
#   .\scripts\gcp_fetch.ps1 -Vehicle GIGA09 -Start "..." -End "..." -AllTopics -ExcludeTopics "/tf*","/events/*"
#   other get_mcap_to_csv.py args via -ExtraArgs: -ExtraArgs "--no-download"

param(
  [string]$Vehicle,
  [string]$Start,
  [string]$End,
  [string]$Topics,
  [switch]$AllTopics,
  [string[]]$ExcludeTopics,
  [switch]$IncludeSensor,
  [switch]$IncludeImage,
  [string[]]$ExtraArgs,
  [string]$EnvFile,
  [string]$GcsFiles,     # JSON list of gs:// mcap paths; skips discovery, extracts exactly these
  [switch]$NoMerged,     # do not write the merged _all.csv
  [string]$LocalOut,     # local folder to receive the CSVs (overrides LOCAL_OUT in gcp.env)
  [switch]$SetupAuth,    # copy local ADC to the VM so it reads GCS as you (first time)
  [switch]$StartStop,    # start before run, stop after (even on error) to minimize cost
  [switch]$DeleteAfter   # delete the VM+disk after (even on error) so idle cost is zero
)

# NOTE: use 'Continue', not 'Stop'. gcloud/plink write normal progress and errors
# (e.g. "Connection refused" while a fresh VM's sshd starts) to stderr, and under
# 'Stop' PowerShell 5.1 turns any native-command stderr line into a terminating
# error. That would abort the SSH-readiness poll on its first attempt. Real
# failures are still caught explicitly via $LASTEXITCODE checks and throw below.
$ErrorActionPreference = 'Continue'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoDir   = Split-Path -Parent $scriptDir

function Read-GcpEnv([string]$path) {
  if (-not (Test-Path $path)) {
    throw "$path not found. Copy scripts\gcp.env.example to scripts\gcp.env and edit it."
  }
  $h = @{}
  foreach ($line in Get-Content -LiteralPath $path) {
    if ($line -match '^\s*#') { continue }
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
      $k = $matches[1]; $v = $matches[2]
      # quoted value -> inside quotes (ignore trailing comment); unquoted -> strip from '#'
      if ($v -match '^\s*"([^"]*)"') { $v = $matches[1] }
      elseif ($v -match "^\s*'([^']*)'") { $v = $matches[1] }
      else { $v = ($v -split '#', 2)[0].Trim() }
      $h[$k] = $v
    }
  }
  return $h
}

if (-not $EnvFile) { $EnvFile = if ($env:GCP_ENV) { $env:GCP_ENV } else { Join-Path $scriptDir 'gcp.env' } }
$cfg = Read-GcpEnv $EnvFile
function Cfg([string]$key, $default = $null) {
  if ($cfg.ContainsKey($key) -and $cfg[$key] -ne '') { return $cfg[$key] } else { return $default }
}

$project   = Cfg 'GCP_PROJECT'; if (-not $project) { throw 'Set GCP_PROJECT in gcp.env' }
$zone      = Cfg 'GCP_ZONE';    if (-not $zone)    { throw 'Set GCP_ZONE in gcp.env' }
$vm        = Cfg 'GCP_VM';      if (-not $vm)      { throw 'Set GCP_VM in gcp.env' }
$remoteDir = Cfg 'REMOTE_DIR' 'GetMcapToCsv'
# strip a leading ~/ (pscp cannot expand it); make it home-relative
$remoteDir = $remoteDir -replace '^~[/\\]', ''
$localOut  = if ($LocalOut) { $LocalOut } else { Cfg 'LOCAL_OUT' 'out' }
$venvDir   = Cfg 'VENV_DIR'
$ros2idl   = Cfg 'ROS2IDL_LOCAL_PATH'

$sshFlags = @()
if ($cfg.ContainsKey('GCLOUD_SSH_FLAGS') -and $cfg['GCLOUD_SSH_FLAGS']) {
  $sshFlags = $cfg['GCLOUD_SSH_FLAGS'] -split '\s+'
}
$baseFlags = @("--project=$project", "--zone=$zone") + $sshFlags

function Invoke-RemoteSsh([string]$cmd) {
  & gcloud compute ssh $vm @baseFlags --command $cmd
  if ($LASTEXITCODE -ne 0) { throw "Remote command failed: $cmd" }
}
function Invoke-Scp($src, $dst, [switch]$Recurse) {
  $a = @('compute', 'scp') + $(if ($Recurse) { @('--recurse') } else { @() }) + $baseFlags + @($src, $dst)
  & gcloud @a
  if ($LASTEXITCODE -ne 0) { throw "Transfer failed: $src -> $dst" }
}
# Pack a folder into a tar and expand on the VM (pscp -r cannot create the remote
# folder and fails; a single-file scp works with pscp).
function Send-Folder($localDir, $remoteParent) {
  $name = Split-Path -Leaf $localDir
  $parent = Split-Path -Parent $localDir
  $tmp = Join-Path $env:TEMP ("gcpfetch_" + $name + ".tgz")
  # exclude heavy/unneeded stuff so a locally-built copy still transfers small
  & tar -czf $tmp '--exclude=.venv' '--exclude=venv' '--exclude=__pycache__' `
      '--exclude=*.egg-info' '--exclude=build' '--exclude=dist' '--exclude=.git' `
      -C $parent $name
  if ($LASTEXITCODE -ne 0) { throw "tar failed: $localDir" }
  Invoke-Scp $tmp "${vm}:$remoteParent/$name.tgz"
  Invoke-RemoteSsh "cd $remoteParent && rm -rf $name && tar -xzf $name.tgz && rm -f $name.tgz"
  Remove-Item $tmp -ErrorAction SilentlyContinue
}
# single-quote for bash (the VM runs bash)
function Q([string]$s) { "'" + ($s -replace "'", "'\''") + "'" }

function Get-VmStatus {
  return (& gcloud compute instances describe $vm "--project=$project" "--zone=$zone" --format='value(status)')
}
function Wait-Ssh {
  # poll until an SSH command succeeds (a freshly created VM needs time for sshd).
  # Swallow every failure (connection refused, key propagation) and keep retrying;
  # only the exit code decides success. 40 x 5s ~= 3 min.
  for ($i = 0; $i -lt 40; $i++) {
    try { & gcloud compute ssh $vm @baseFlags --command 'true' 2>&1 | Out-Null } catch { }
    if ($LASTEXITCODE -eq 0) { return $true }
    Start-Sleep -Seconds 5
  }
  return $false
}
function Wait-VmReady {
  $st = Get-VmStatus
  if ($st -ne 'RUNNING') {
    Write-Host "[info] Starting VM... (was: $st)"
    & gcloud compute instances start $vm "--project=$project" "--zone=$zone" | Out-Null
  } else {
    Write-Host '[info] VM already running.'
  }
  # Always wait for SSH: a just-created VM is RUNNING but sshd may not accept yet
  Write-Host '[info] Waiting for SSH to be ready...'
  if (-not (Wait-Ssh)) { Write-Warning 'SSH not ready after waiting; continuing (may fail).' }
}
function Stop-VmNow {
  Write-Host '[info] Stopping VM (halt billing)...'
  & gcloud compute instances stop $vm "--project=$project" "--zone=$zone" | Out-Null
  Write-Host '[ok] VM stopped.'
}
function Remove-VmNow {
  Write-Host '[info] Deleting VM and its disk (zero standing cost)...'
  & gcloud compute instances delete $vm "--project=$project" "--zone=$zone" --quiet | Out-Null
  Write-Host '[ok] VM deleted. Recreate with gcp_create_vm.ps1 next time.'
}

try {
# Always ensure the VM is up and sshd is accepting connections before the first
# SSH/SCP. A just-created VM reports RUNNING but sshd may still refuse the first
# connection ("Connection refused"), so we poll until SSH works.
Wait-VmReady

Write-Host "[info] Pushing tool to VM ($vm / $zone)..."
Invoke-RemoteSsh "mkdir -p $remoteDir/scripts"
foreach ($f in @('get_mcap_to_csv.py', 'requirements.txt', 'topics.example.t2.json', 'topics.example.apollo.json')) {
  Invoke-Scp (Join-Path $repoDir $f) "${vm}:$remoteDir/$f"
}
Invoke-Scp (Join-Path $repoDir 'scripts\run_on_gcp.sh') "${vm}:$remoteDir/scripts/run_on_gcp.sh"
# strip CRLF added by Windows Git checkout (bash fails on pipefail\r etc.)
Invoke-RemoteSsh "sed -i 's/\r//' $remoteDir/scripts/run_on_gcp.sh"

# transfer a user-specified topics file and reference it by basename on the VM
$remoteTopics = $null
if ($Topics) {
  if (-not (Test-Path $Topics)) { throw "topics file not found: $Topics" }
  $tname = Split-Path -Leaf $Topics
  Invoke-Scp $Topics "${vm}:$remoteDir/$tname"
  $remoteTopics = $tname
}

# transfer the explicit file list (UI selection) the same way
$remoteGcsFiles = $null
if ($GcsFiles) {
  if (-not (Test-Path $GcsFiles)) { throw "gcs-files list not found: $GcsFiles" }
  $gname = Split-Path -Leaf $GcsFiles
  Invoke-Scp $GcsFiles "${vm}:$remoteDir/$gname"
  $remoteGcsFiles = $gname
}

# send the /t2 decode package to the VM (avoids GitHub auth on the VM)
$envExport = ''
if ($venvDir) { $envExport += "VENV_DIR=$(Q $venvDir) " }
if ($ros2idl) {
  if (Test-Path $ros2idl) {
    $ros2idlFull = (Resolve-Path $ros2idl).Path
    Write-Host "[info] Sending mcap-ros2idl-support to VM from: $ros2idlFull"
    # client-side check: is THIS local folder apex_json-capable? (points out a wrong/old path)
    $facLocal = Get-ChildItem -Path $ros2idlFull -Recurse -Filter decode_factory.py -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($facLocal) {
      if (Select-String -Path $facLocal.FullName -Pattern 'apex_json' -Quiet) {
        Write-Host '[info] local source supports apex_json (good).'
      } else {
        Write-Warning "This local mcap-ros2idl-support is OLD (no apex_json): $($facLocal.FullName)"
        Write-Warning 'Update it (git pull in that repo) or fix ROS2IDL_LOCAL_PATH in gcp.env.'
      }
    } else {
      Write-Warning "decode_factory.py not found under $ros2idlFull (is ROS2IDL_LOCAL_PATH correct?)"
    }
    Send-Folder $ros2idl $remoteDir
    # run_on_gcp.sh cd's to the repo root ($remoteDir), so pass a path relative to it
    $envExport += "ROS2IDL_PATH=$(Q 'mcap-ros2idl-support') "
  } else {
    Write-Warning "ROS2IDL_LOCAL_PATH not found: $ros2idl (skipping)"
  }
}

# copy local ADC (application-default creds) to the VM so it reads GCS as you,
# not as the VM service account (no bucket IAM change needed).
if ($SetupAuth) {
  $adc = if ($env:GOOGLE_APPLICATION_CREDENTIALS) { $env:GOOGLE_APPLICATION_CREDENTIALS }
         else { Join-Path $env:APPDATA 'gcloud\application_default_credentials.json' }
  if (-not (Test-Path $adc)) {
    throw @"
Local ADC not found: $adc
Run this first (opens a browser):
  gcloud auth application-default login
Then run again with -SetupAuth.
"@
  }
  Write-Host '[info] Copying local ADC to the VM...'
  Invoke-RemoteSsh 'mkdir -p .config/gcloud'
  Invoke-Scp $adc "${vm}:.config/gcloud/application_default_credentials.json"
  Write-Host '[ok] VM now reads GCS with your credentials.'
}

# build extraction args
$ra = @()
if ($Vehicle)       { $ra += @('--vehicle', $Vehicle) }
if ($Start)         { $ra += @('--start', $Start) }
if ($End)           { $ra += @('--end', $End) }
if ($AllTopics)     { $ra += '--all-topics' }
if ($remoteTopics)  { $ra += @('--topics', $remoteTopics) }
if ($remoteGcsFiles) { $ra += @('--gcs-files', $remoteGcsFiles) }
if ($ExcludeTopics) { $ra += '--exclude-topics'; $ra += $ExcludeTopics }
if ($IncludeSensor) { $ra += '--include-sensor' }
if ($IncludeImage)  { $ra += '--include-image' }
if ($NoMerged)      { $ra += '--no-merged' }
if ($ExtraArgs)     { $ra += $ExtraArgs }
if ($ra.Count -eq 0) { throw 'No extraction args. Pass -Vehicle/-Start/-End/-Topics etc.' }

$remoteArgs = ($ra | ForEach-Object { Q $_ }) -join ' '
$runCmd = "cd $remoteDir && $envExport" + "bash scripts/run_on_gcp.sh $remoteArgs"

# list/topics/estimate modes produce no CSV; run only
$noCsvMode = @('--list-only', '--list-topics', '--estimate') | Where-Object { $ra -contains $_ }

Write-Host '[info] Running extraction on the VM (GCS read is egress-free)...'
Invoke-RemoteSsh $runCmd

if ($noCsvMode) {
  Write-Host '[ok] Done (list/estimate mode: no CSV download).'
}
else {
  Write-Host '[info] Downloading CSV...'
  New-Item -ItemType Directory -Force -Path $localOut | Out-Null
  $archive = Join-Path $localOut 'out_csv.tar.gz'
  Invoke-Scp "${vm}:$remoteDir/out_csv.tar.gz" $archive
  # list exactly what THIS run produced (archive contents) so old files already in
  # the output folder are not mistaken for this run's output
  $produced = & tar -tzf $archive |
    ForEach-Object { ($_ -replace '^\./', '').Trim('/') } |
    Where-Object { $_ -match '\.csv$' } | Sort-Object
  # Windows 10+ ships tar.exe
  & tar -xzf $archive -C $localOut
  Remove-Item $archive -ErrorAction SilentlyContinue
  # manifest of this run's files (app.py reads it to show only new output)
  Set-Content -Path (Join-Path $localOut '_last_run.txt') -Value $produced -Encoding UTF8

  Write-Host "[ok] Done. This run produced $($produced.Count) CSV file(s) in $localOut\:"
  $produced | ForEach-Object { Write-Host "  $_" }
}

}
finally {
  if ($DeleteAfter) { Remove-VmNow }
  elseif ($StartStop) { Stop-VmNow }
}
