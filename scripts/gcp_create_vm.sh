#!/usr/bin/env bash
# バケットと同一リージョン (東京) に、mcap->CSV 変換用の小さな VM を1台作る。
# gcp.env の GCP_PROJECT / GCP_ZONE / GCP_VM を読む。
#
# 使い方:
#   cp scripts/gcp.env.example scripts/gcp.env   # 編集
#   bash scripts/gcp_create_vm.sh
#
# 作成後は scripts/gcp_fetch.sh で抽出できる。使い終わったら停止:
#   gcloud compute instances stop <VM> --zone <ZONE> --project <PROJECT>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${GCP_ENV:-$SCRIPT_DIR/gcp.env}"
[ -f "$ENV_FILE" ] || { echo "[error] $ENV_FILE がありません。gcp.env.example からコピーしてください。"; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${GCP_PROJECT:?gcp.env に GCP_PROJECT を設定してください}"
: "${GCP_ZONE:?gcp.env に GCP_ZONE を設定してください}"
: "${GCP_VM:?gcp.env に GCP_VM を設定してください}"

if [ "$GCP_PROJECT" = "your-project-id" ] || [ "$GCP_VM" = "your-vm-name" ]; then
  echo "[error] gcp.env が未編集です (GCP_PROJECT / GCP_VM がサンプルのまま)。実際の値に書き換えてください。"
  echo "        現在の GCP_PROJECT = '$GCP_PROJECT'"
  echo "        使えるプロジェクト: gcloud projects list"
  echo "        既定プロジェクト  : gcloud config get-value project"
  exit 1
fi

# 既定値 (必要なら gcp.env で上書き可能)
MACHINE_TYPE="${MACHINE_TYPE:-e2-standard-4}"     # 4 vCPU / 16GB。並列デコードに十分
BOOT_DISK_GB="${BOOT_DISK_GB:-30}"                # OS + 一時ファイルに十分。停止中ディスク代を抑える (30GB≒月¥450)
IMAGE_FAMILY="${IMAGE_FAMILY:-debian-12}"
IMAGE_PROJECT="${IMAGE_PROJECT:-debian-cloud}"
# PRIVATE=0 (既定): 外部IP付きの標準的な VM (SSH は IAM/鍵で保護)。手順が単純で詰まりにくい。
# PRIVATE=1 にすると外部IPなし + IAP SSH + OS Login でネットワーク面を最小公開にする
#   (IAP 用ファイアウォールを作るため、組織ポリシー/権限によっては失敗しうる)。
PRIVATE="${PRIVATE:-0}"
IAP_TAG="${IAP_TAG:-mcap-iap-ssh}"                # IAP SSH 許可の対象タグ

net_args=()
if [ "$PRIVATE" = "1" ]; then
  net_args=(--no-address
            --tags="$IAP_TAG"
            --metadata=enable-oslogin=TRUE
            --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring)
  # 以降の gcloud ssh/scp は IAP トンネル必須になる
  export GCLOUD_SSH_FLAGS="${GCLOUD_SSH_FLAGS:-} --tunnel-through-iap"
fi

echo "[info] VM を作成します:"
echo "  プロジェクト : $GCP_PROJECT"
echo "  ゾーン       : $GCP_ZONE"
echo "  名前         : $GCP_VM"
echo "  マシン       : $MACHINE_TYPE / ブートディスク ${BOOT_DISK_GB}GB / $IMAGE_FAMILY"
echo "  権限         : バケット読み取り (devstorage.read_only)"
if [ "$PRIVATE" = "1" ]; then
  echo "  公開範囲     : 最小 (外部IPなし / IAP SSH / OS Login)  ← あなた以外は中に入れない"
else
  echo "  公開範囲     : 標準 (外部IPあり / SSH は IAM で保護)"
fi
read -r -p "作成しますか? [y/N] " ans
[ "$ans" = "y" ] || [ "$ans" = "Y" ] || { echo "中止しました。"; exit 0; }

# PRIVATE 時は IAP からの SSH を許可するファイアウォールを用意 (無ければ作る。best-effort)
if [ "$PRIVATE" = "1" ]; then
  FW_RULE="${FW_RULE:-allow-iap-ssh-$IAP_TAG}"
  if ! gcloud compute firewall-rules describe "$FW_RULE" --project="$GCP_PROJECT" >/dev/null 2>&1; then
    echo "[info] IAP SSH 用ファイアウォールを作成: $FW_RULE"
    gcloud compute firewall-rules create "$FW_RULE" \
      --project="$GCP_PROJECT" \
      --direction=INGRESS --action=ALLOW --rules=tcp:22 \
      --source-ranges=35.235.240.0/20 \
      --target-tags="$IAP_TAG" \
      || echo "[warn] ファイアウォール作成に失敗 (権限/組織ポリシー)。管理者に tcp:22 from 35.235.240.0/20 の許可を依頼してください。"
  fi
fi

gcloud compute instances create "$GCP_VM" \
  --project="$GCP_PROJECT" \
  --zone="$GCP_ZONE" \
  --machine-type="$MACHINE_TYPE" \
  --image-family="$IMAGE_FAMILY" \
  --image-project="$IMAGE_PROJECT" \
  --boot-disk-size="${BOOT_DISK_GB}GB" \
  --boot-disk-type=pd-balanced \
  --scopes=https://www.googleapis.com/auth/devstorage.read_only \
  "${net_args[@]}"

echo
echo "[ok] 作成しました。初回のみ Python を用意します..."
# Debian 12 は python3 同梱。venv/pip 用に python3-venv を入れておく
gcloud compute ssh "$GCP_VM" --project="$GCP_PROJECT" --zone="$GCP_ZONE" ${GCLOUD_SSH_FLAGS:-} \
  --command="sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv python3-pip >/dev/null && python3 --version"

echo
if [ "$PRIVATE" = "1" ]; then
  echo "[info] このVMは外部IPなし + IAP SSH です。gcp_fetch.sh も自動で IAP を使うよう、"
  echo "       gcp.env に次の1行を入れておいてください (未設定なら追記推奨):"
  echo '         GCLOUD_SSH_FLAGS="--tunnel-through-iap"'
fi
echo "[ok] 準備完了。抽出は次で実行できます:"
echo "  bash scripts/gcp_fetch.sh --vehicle GIGA09 --start \"2026-07-01 20:40\" --end \"2026-07-01 20:45\" --topics topics.example.t2.json"
echo
echo "使い終わったら VM を止めて課金を抑えてください:"
echo "  gcloud compute instances stop $GCP_VM --zone $GCP_ZONE --project $GCP_PROJECT"
