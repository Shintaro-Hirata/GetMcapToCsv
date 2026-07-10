#!/usr/bin/env bash
# 「今、課金対象のリソースが残っていないか」を確認する (読み取りのみ・安全)。
# モデルB (毎回作って消す) で、削除し忘れ・クラッシュで VM が残っていないかの点検用。
#   bash scripts/gcp_status.sh
# 何も (VM 名の行が) 表示されなければ、この用途での課金はありません。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${GCP_ENV:-$SCRIPT_DIR/gcp.env}"
[ -f "$ENV_FILE" ] || { echo "[error] $ENV_FILE がありません。"; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${GCP_PROJECT:?gcp.env に GCP_PROJECT を設定してください}"
: "${GCP_VM:?gcp.env に GCP_VM を設定してください}"

echo "=== 課金対象リソースの点検 (project: $GCP_PROJECT) ==="
echo
echo "--- VM インスタンス (name=$GCP_VM) ---  ※ STATUS が RUNNING なら計算課金中"
gcloud compute instances list --project "$GCP_PROJECT" --filter="name=$GCP_VM" --format='table(name,zone,status)'
echo
echo "--- ディスク (name=$GCP_VM) ---  ※ 存在するだけで (停止中でも) 課金"
gcloud compute disks list --project "$GCP_PROJECT" --filter="name=$GCP_VM" --format='table(name,zone,sizeGb,status)'
echo
echo "--- 予約済み外部IP ---  ※ 未使用でも課金 (通常は無いはず)"
gcloud compute addresses list --project "$GCP_PROJECT" --format='table(name,region,address,status)'
echo
echo "上に $GCP_VM の行が無ければ、この用途での課金はありません。"
echo "残っていた場合の削除:"
echo "  gcloud compute instances delete $GCP_VM --project $GCP_PROJECT --zone <ZONE>"
