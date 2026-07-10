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

# 既定値 (必要なら gcp.env で上書き可能)
MACHINE_TYPE="${MACHINE_TYPE:-e2-standard-4}"     # 4 vCPU / 16GB。並列デコードに十分
BOOT_DISK_GB="${BOOT_DISK_GB:-100}"               # キャッシュ + 一時ファイル用に余裕を持たせる
IMAGE_FAMILY="${IMAGE_FAMILY:-debian-12}"
IMAGE_PROJECT="${IMAGE_PROJECT:-debian-cloud}"

echo "[info] VM を作成します:"
echo "  プロジェクト : $GCP_PROJECT"
echo "  ゾーン       : $GCP_ZONE"
echo "  名前         : $GCP_VM"
echo "  マシン       : $MACHINE_TYPE / ブートディスク ${BOOT_DISK_GB}GB / $IMAGE_FAMILY"
echo "  権限         : バケット読み取り (devstorage.read_only)"
read -r -p "作成しますか? [y/N] " ans
[ "$ans" = "y" ] || [ "$ans" = "Y" ] || { echo "中止しました。"; exit 0; }

gcloud compute instances create "$GCP_VM" \
  --project="$GCP_PROJECT" \
  --zone="$GCP_ZONE" \
  --machine-type="$MACHINE_TYPE" \
  --image-family="$IMAGE_FAMILY" \
  --image-project="$IMAGE_PROJECT" \
  --boot-disk-size="${BOOT_DISK_GB}GB" \
  --boot-disk-type=pd-balanced \
  --scopes=https://www.googleapis.com/auth/devstorage.read_only

echo
echo "[ok] 作成しました。初回のみ Python を用意します..."
# Debian 12 は python3 同梱。venv/pip 用に python3-venv を入れておく
gcloud compute ssh "$GCP_VM" --project="$GCP_PROJECT" --zone="$GCP_ZONE" ${GCLOUD_SSH_FLAGS:-} \
  --command="sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv python3-pip >/dev/null && python3 --version"

echo
echo "[ok] 準備完了。抽出は次で実行できます:"
echo "  bash scripts/gcp_fetch.sh --vehicle GIGA09 --start \"2026-07-01 20:40\" --end \"2026-07-01 20:45\" --topics topics.example.t2.json"
echo
echo "使い終わったら VM を止めて課金を抑えてください:"
echo "  gcloud compute instances stop $GCP_VM --zone $GCP_ZONE --project $GCP_PROJECT"
