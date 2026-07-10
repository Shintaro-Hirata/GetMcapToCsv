# scripts/gcp_create_vm.ps1
# PowerShell 版の VM 作成スクリプト (bash 不要)。
# バケットと同一リージョン (東京) に mcap->CSV 変換用の小さな VM を1台作る。
# scripts/gcp.env を読む。
#
# 使い方 (PowerShell):
#   Copy-Item scripts\gcp.env.example scripts\gcp.env   # 編集
#   .\scripts\gcp_create_vm.ps1
#
# 使い終わったら停止:
#   gcloud compute instances stop <VM> --zone <ZONE> --project <PROJECT>
param(
  [switch]$Yes   # 確認プロンプトを出さずに作成する (UI/自動実行から呼ぶ用)
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

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

$envFile = if ($env:GCP_ENV) { $env:GCP_ENV } else { Join-Path $scriptDir 'gcp.env' }
$cfg = Read-GcpEnv $envFile

function Cfg([string]$key, $default = $null) {
  if ($cfg.ContainsKey($key) -and $cfg[$key] -ne '') { return $cfg[$key] } else { return $default }
}

$project = Cfg 'GCP_PROJECT'; if (-not $project) { throw 'gcp.env に GCP_PROJECT を設定してください' }
$zone    = Cfg 'GCP_ZONE';    if (-not $zone)    { throw 'gcp.env に GCP_ZONE を設定してください' }
$vm      = Cfg 'GCP_VM';      if (-not $vm)      { throw 'gcp.env に GCP_VM を設定してください' }

if ($project -eq 'your-project-id' -or $vm -eq 'your-vm-name') {
  throw @"
gcp.env が未編集です (GCP_PROJECT / GCP_VM がサンプルのまま)。実際の値に書き換えてください。
  現在の GCP_PROJECT = '$project'
使えるプロジェクトの一覧:  gcloud projects list
既定のプロジェクト確認  :  gcloud config get-value project
"@
}

$machineType = Cfg 'MACHINE_TYPE' 'e2-standard-4'
$bootDiskGb  = Cfg 'BOOT_DISK_GB' '30'   # OS + 一時ファイルに十分。停止中ディスク代を抑える (30GB≒月¥450)
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

Write-Host '[info] VM を作成します:'
Write-Host "  プロジェクト : $project"
Write-Host "  ゾーン       : $zone"
Write-Host "  名前         : $vm"
Write-Host "  マシン       : $machineType / ブートディスク ${bootDiskGb}GB / $imageFamily"
Write-Host '  権限         : バケット読み取り (devstorage.read_only)'
if ($private) {
  Write-Host '  公開範囲     : 最小 (外部IPなし / IAP SSH / OS Login)'
} else {
  Write-Host '  公開範囲     : 標準 (外部IPあり / SSH は IAM で保護)'
}
if (-not $Yes) {
  $ans = Read-Host '作成しますか? [y/N]'
  if ($ans -ne 'y' -and $ans -ne 'Y') { Write-Host '中止しました。'; exit 0 }
}

if ($private) {
  $fwRule = Cfg 'FW_RULE' "allow-iap-ssh-$iapTag"
  & gcloud compute firewall-rules describe $fwRule --project=$project 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Write-Host "[info] IAP SSH 用ファイアウォールを作成: $fwRule"
    & gcloud compute firewall-rules create $fwRule --project=$project `
      --direction=INGRESS --action=ALLOW --rules=tcp:22 `
      --source-ranges=35.235.240.0/20 --target-tags=$iapTag
    if ($LASTEXITCODE -ne 0) {
      Write-Warning 'ファイアウォール作成に失敗 (権限/組織ポリシー)。管理者に tcp:22 from 35.235.240.0/20 の許可を依頼してください。'
    }
  }
}

$createArgs = @('compute', 'instances', 'create', $vm,
  "--project=$project", "--zone=$zone", "--machine-type=$machineType",
  "--image-family=$imageFamily", "--image-project=$imageProj",
  "--boot-disk-size=${bootDiskGb}GB", '--boot-disk-type=pd-balanced',
  '--scopes=https://www.googleapis.com/auth/devstorage.read_only') + $netArgs
& gcloud @createArgs
if ($LASTEXITCODE -ne 0) { throw 'VM 作成に失敗しました。上のエラーを確認してください。' }

Write-Host ''
Write-Host '[ok] 作成しました。初回のみ Python を用意します...'
$installCmd = 'sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv python3-pip >/dev/null && python3 --version'
$sshArgs = @('compute', 'ssh', $vm, "--project=$project", "--zone=$zone") + $sshFlags + @('--command', $installCmd)
& gcloud @sshArgs

Write-Host ''
if ($private) {
  Write-Host '[info] このVMは外部IPなし + IAP SSH です。gcp.env に次を入れておいてください:'
  Write-Host '         GCLOUD_SSH_FLAGS="--tunnel-through-iap"'
}
Write-Host '[ok] 準備完了。抽出は次で実行できます:'
Write-Host '  .\scripts\gcp_fetch.ps1 -Vehicle GIGA09 -Start "2026-07-01 20:40" -End "2026-07-01 20:45" -Topics topics.example.t2.json'
Write-Host ''
Write-Host '使い終わったら VM を止めて課金を抑えてください:'
Write-Host "  gcloud compute instances stop $vm --zone $zone --project $project"
