#!/usr/bin/env bash
# 手元 PC から実行する司令塔スクリプト。
#   1. 最新のツール一式を VM へ転送
#   2. VM 上で mcap -> CSV 変換 (GCS 読み込みは VM 内 = egress 無料)
#   3. 出来た CSV (小さい) だけを手元へダウンロード
#
# これにより「重い mcap は GCP の中に留め、外に出るのは軽い CSV だけ」になり、
# ネットワーク課金を 95〜99% 削減できる。
#
# 前提: gcloud CLI が入っていて認証済み。scripts/gcp.env を用意しておく。
# 使い方 (Git Bash / WSL / macOS / Linux):
#   bash scripts/gcp_fetch.sh --vehicle GIGA09 --start "2026-07-01 20:40" \
#       --end "2026-07-01 20:45" --topics topics.example.t2.json
# get_mcap_to_csv.py への引数はそのまま渡る。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${GCP_ENV:-$SCRIPT_DIR/gcp.env}"

if [ ! -f "$ENV_FILE" ]; then
  echo "[error] 設定ファイルがありません: $ENV_FILE"
  echo "        cp scripts/gcp.env.example scripts/gcp.env して編集してください。"
  exit 1
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${GCP_PROJECT:?gcp.env に GCP_PROJECT を設定してください}"
: "${GCP_ZONE:?gcp.env に GCP_ZONE を設定してください}"
: "${GCP_VM:?gcp.env に GCP_VM を設定してください}"
# ホーム相対パス (先頭 ~/ は付けない。Windows 版と揃え、scp の ~ 展開問題も避ける)
REMOTE_DIR="${REMOTE_DIR:-GetMcapToCsv}"
REMOTE_DIR="${REMOTE_DIR#\~/}"
LOCAL_OUT="${LOCAL_OUT:-out}"

SSH_FLAGS=(--project "$GCP_PROJECT" --zone "$GCP_ZONE" ${GCLOUD_SSH_FLAGS:-})
REMOTE="$GCP_VM"

run_ssh() { gcloud compute ssh "$REMOTE" "${SSH_FLAGS[@]}" --command "$1"; }

echo "[info] VM ($GCP_VM / $GCP_ZONE) にツールを転送..."
run_ssh "mkdir -p $REMOTE_DIR/scripts"
# 実行に必要な最小ファイルだけ転送 (mcap 本体は転送しない)
for f in get_mcap_to_csv.py requirements.txt topics.example.t2.json topics.example.apollo.json; do
  gcloud compute scp "${SSH_FLAGS[@]}" "$REPO_DIR/$f" "$REMOTE:$REMOTE_DIR/$f"
done
gcloud compute scp "${SSH_FLAGS[@]}" "$REPO_DIR/scripts/run_on_gcp.sh" "$REMOTE:$REMOTE_DIR/scripts/run_on_gcp.sh"
# CRLF チェックアウト対策 (bash が pipefail\r 等で失敗するため VM 側で除去)
run_ssh "sed -i 's/\r//' $REMOTE_DIR/scripts/run_on_gcp.sh"

# /t2 デコード用の私物パッケージを手元から VM へ転送 (VM での GitHub 認証を回避)
ROS2IDL_REMOTE=""
if [ -n "${ROS2IDL_LOCAL_PATH:-}" ]; then
  if [ -d "$ROS2IDL_LOCAL_PATH" ]; then
    echo "[info] mcap-ros2idl-support を VM へ転送..."
    run_ssh "rm -rf $REMOTE_DIR/mcap-ros2idl-support"
    gcloud compute scp --recurse "${SSH_FLAGS[@]}" "$ROS2IDL_LOCAL_PATH" \
      "$REMOTE:$REMOTE_DIR/mcap-ros2idl-support"
    # run_on_gcp.sh はリポジトリルート ($REMOTE_DIR) に cd するので、そこからの相対で渡す
    ROS2IDL_REMOTE="mcap-ros2idl-support"
  else
    echo "[warn] ROS2IDL_LOCAL_PATH が見つかりません: $ROS2IDL_LOCAL_PATH (スキップ)"
  fi
fi

# 引数を安全に 1 つの文字列へ (空白を含む時刻等をクォート)
ARGS=""
for a in "$@"; do
  ARGS+=" $(printf '%q' "$a")"
done
VENV_EXPORT=""
if [ -n "${VENV_DIR:-}" ]; then
  VENV_EXPORT="VENV_DIR=$(printf '%q' "$VENV_DIR") "
fi
if [ -n "$ROS2IDL_REMOTE" ]; then
  VENV_EXPORT+="ROS2IDL_PATH=$(printf '%q' "$ROS2IDL_REMOTE") "
fi

echo "[info] VM 上で抽出を実行 (GCS 読み込みは egress 無料)..."
run_ssh "cd $REMOTE_DIR && ${VENV_EXPORT}bash scripts/run_on_gcp.sh$ARGS"

echo "[info] CSV を手元へダウンロード..."
mkdir -p "$LOCAL_OUT"
gcloud compute scp "${SSH_FLAGS[@]}" "$REMOTE:$REMOTE_DIR/out_csv.tar.gz" "$LOCAL_OUT/out_csv.tar.gz"
tar -xzf "$LOCAL_OUT/out_csv.tar.gz" -C "$LOCAL_OUT"
rm -f "$LOCAL_OUT/out_csv.tar.gz"

echo "[ok] 完了。CSV は $LOCAL_OUT/ に展開しました:"
ls -1 "$LOCAL_OUT"/*.csv 2>/dev/null || echo "  (CSV が見つかりません。VM 側のログを確認してください)"
