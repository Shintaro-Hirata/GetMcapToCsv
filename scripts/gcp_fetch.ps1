# scripts/gcp_fetch.ps1
# PowerShell 版の司令塔スクリプト (bash 不要)。
#   1. ツール一式を VM へ転送 (私物パッケージ mcap-ros2idl-support 含む)
#   2. VM 上で mcap->CSV 変換 (GCS 読み込みは VM 内 = egress 無料)
#   3. 出来た CSV (小さい) だけを手元へダウンロード
#
# 前提: gcloud CLI が入っていて認証済み。scripts\gcp.env を用意しておく。
# 使い方 (PowerShell):
#   .\scripts\gcp_fetch.ps1 -Vehicle GIGA09 -Start "2026-07-01 20:40" -End "2026-07-01 20:45" -Topics topics.example.t2.json
#   .\scripts\gcp_fetch.ps1 -Vehicle GIGA09 -Start "..." -End "..." -AllTopics -IncludeSensor
#   .\scripts\gcp_fetch.ps1 -Vehicle GIGA09 -Start "..." -End "..." -AllTopics -ExcludeTopics "/tf*","/events/*"
#   その他の get_mcap_to_csv.py 引数は -ExtraArgs で渡す: -ExtraArgs "--no-download"

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
  [string]$EnvFile
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoDir   = Split-Path -Parent $scriptDir

function Read-GcpEnv([string]$path) {
  if (-not (Test-Path $path)) {
    throw "$path がありません。scripts\gcp.env.example からコピーして編集してください。"
  }
  $h = @{}
  foreach ($line in Get-Content -LiteralPath $path) {
    if ($line -match '^\s*#') { continue }
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
      $k = $matches[1]; $v = $matches[2]
      # クォート値は中身を採用 (行末コメントは無視)、非クォートは最初の # 以降を除去
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

$project   = Cfg 'GCP_PROJECT'; if (-not $project) { throw 'gcp.env に GCP_PROJECT を設定してください' }
$zone      = Cfg 'GCP_ZONE';    if (-not $zone)    { throw 'gcp.env に GCP_ZONE を設定してください' }
$vm        = Cfg 'GCP_VM';      if (-not $vm)      { throw 'gcp.env に GCP_VM を設定してください' }
$remoteDir = Cfg 'REMOTE_DIR' '~/GetMcapToCsv'
$localOut  = Cfg 'LOCAL_OUT' 'out'
$venvDir   = Cfg 'VENV_DIR'
$ros2idl   = Cfg 'ROS2IDL_LOCAL_PATH'

$sshFlags = @()
if ($cfg.ContainsKey('GCLOUD_SSH_FLAGS') -and $cfg['GCLOUD_SSH_FLAGS']) {
  $sshFlags = $cfg['GCLOUD_SSH_FLAGS'] -split '\s+'
}
$baseFlags = @("--project=$project", "--zone=$zone") + $sshFlags

function Invoke-RemoteSsh([string]$cmd) {
  & gcloud compute ssh $vm @baseFlags --command $cmd
  if ($LASTEXITCODE -ne 0) { throw "リモート実行に失敗しました: $cmd" }
}
function Invoke-Scp($src, $dst, [switch]$Recurse) {
  $a = @('compute', 'scp') + $(if ($Recurse) { @('--recurse') } else { @() }) + $baseFlags + @($src, $dst)
  & gcloud @a
  if ($LASTEXITCODE -ne 0) { throw "転送に失敗しました: $src -> $dst" }
}
# bash 用に単一引用符でクォート (VM 側は bash で受ける)
function Q([string]$s) { "'" + ($s -replace "'", "'\''") + "'" }

Write-Host "[info] VM ($vm / $zone) にツールを転送..."
Invoke-RemoteSsh "mkdir -p $remoteDir/scripts"
foreach ($f in @('get_mcap_to_csv.py', 'requirements.txt', 'topics.example.t2.json', 'topics.example.apollo.json')) {
  Invoke-Scp (Join-Path $repoDir $f) "${vm}:$remoteDir/$f"
}
Invoke-Scp (Join-Path $repoDir 'scripts\run_on_gcp.sh') "${vm}:$remoteDir/scripts/run_on_gcp.sh"

# ユーザー指定の topics ファイルがあれば転送し、リモートではその basename を使う
$remoteTopics = $null
if ($Topics) {
  if (-not (Test-Path $Topics)) { throw "topics ファイルが見つかりません: $Topics" }
  $tname = Split-Path -Leaf $Topics
  Invoke-Scp $Topics "${vm}:$remoteDir/$tname"
  $remoteTopics = $tname
}

# /t2 デコード用パッケージを VM へ転送 (VM 側 GitHub 認証を回避)
$envExport = ''
if ($venvDir) { $envExport += "VENV_DIR=$(Q $venvDir) " }
if ($ros2idl) {
  if (Test-Path $ros2idl) {
    Write-Host '[info] mcap-ros2idl-support を VM へ転送...'
    Invoke-RemoteSsh "rm -rf $remoteDir/mcap-ros2idl-support"
    Invoke-Scp $ros2idl "${vm}:$remoteDir/mcap-ros2idl-support" -Recurse
    $envExport += "ROS2IDL_PATH=$(Q "$remoteDir/mcap-ros2idl-support") "
  } else {
    Write-Warning "ROS2IDL_LOCAL_PATH が見つかりません: $ros2idl (スキップ)"
  }
}

# 抽出引数を組み立てる
$ra = @()
if ($Vehicle)       { $ra += @('--vehicle', $Vehicle) }
if ($Start)         { $ra += @('--start', $Start) }
if ($End)           { $ra += @('--end', $End) }
if ($AllTopics)     { $ra += '--all-topics' }
if ($remoteTopics)  { $ra += @('--topics', $remoteTopics) }
if ($ExcludeTopics) { $ra += '--exclude-topics'; $ra += $ExcludeTopics }
if ($IncludeSensor) { $ra += '--include-sensor' }
if ($IncludeImage)  { $ra += '--include-image' }
if ($ExtraArgs)     { $ra += $ExtraArgs }
if ($ra.Count -eq 0) { throw '抽出引数がありません。-Vehicle/-Start/-End/-Topics 等を指定してください。' }

$remoteArgs = ($ra | ForEach-Object { Q $_ }) -join ' '
$runCmd = "cd $remoteDir && $envExport" + "bash scripts/run_on_gcp.sh $remoteArgs"

Write-Host '[info] VM 上で抽出を実行 (GCS 読み込みは egress 無料)...'
Invoke-RemoteSsh $runCmd

Write-Host '[info] CSV を手元へダウンロード...'
New-Item -ItemType Directory -Force -Path $localOut | Out-Null
$archive = Join-Path $localOut 'out_csv.tar.gz'
Invoke-Scp "${vm}:$remoteDir/out_csv.tar.gz" $archive
# Windows 10+ は tar.exe 同梱
& tar -xzf $archive -C $localOut
Remove-Item $archive -ErrorAction SilentlyContinue

Write-Host "[ok] 完了。CSV は $localOut\ に展開しました:"
Get-ChildItem -Path $localOut -Filter *.csv | ForEach-Object { Write-Host "  $($_.Name)" }
