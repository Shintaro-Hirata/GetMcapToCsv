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

# --setup-auth / --start-stop / --delete-after / --local-out を取り出す
# (残りは get_mcap_to_csv.py へ渡す)
SETUP_AUTH=0
START_STOP=0
DELETE_AFTER=0
TOPICS_FILE=""
GCS_FILES_FILE=""
POS_ARGS=()
EXPECT_LOCAL_OUT=0
EXPECT_TOPICS=0
EXPECT_GCS_FILES=0
for a in "$@"; do
  if [ "$EXPECT_LOCAL_OUT" = "1" ]; then LOCAL_OUT="$a"; EXPECT_LOCAL_OUT=0; continue; fi
  if [ "$EXPECT_TOPICS" = "1" ]; then TOPICS_FILE="$a"; EXPECT_TOPICS=0; continue; fi
  if [ "$EXPECT_GCS_FILES" = "1" ]; then GCS_FILES_FILE="$a"; EXPECT_GCS_FILES=0; continue; fi
  case "$a" in
    --setup-auth) SETUP_AUTH=1 ;;
    --start-stop) START_STOP=1 ;;
    --delete-after) DELETE_AFTER=1 ;;
    --local-out) EXPECT_LOCAL_OUT=1 ;;
    --topics) EXPECT_TOPICS=1 ;;      # 手元の topics JSON は VM へ転送してから basename で渡す
    --gcs-files) EXPECT_GCS_FILES=1 ;;  # ②選択のファイル一覧 JSON も同様に転送する
    *) POS_ARGS+=("$a") ;;
  esac
done
set -- "${POS_ARGS[@]}"

# --start-stop / --delete-after: 終了時 (エラー時も) 停止 or 削除する trap を仕掛ける
if [ "$START_STOP" = "1" ] || [ "$DELETE_AFTER" = "1" ]; then
  cleanup_vm() {
    if [ "$DELETE_AFTER" = "1" ]; then
      echo "[info] VM をディスクごと削除します (以後の標準料金をゼロに)..."
      gcloud compute instances delete "$REMOTE" --project "$GCP_PROJECT" --zone "$GCP_ZONE" --quiet >/dev/null \
        && echo "[ok] VM を削除しました。次回は gcp_create_vm.sh で作り直してください。"
    else
      echo "[info] VM を停止します (課金を止める)..."
      gcloud compute instances stop "$REMOTE" --project "$GCP_PROJECT" --zone "$GCP_ZONE" >/dev/null \
        && echo "[ok] VM を停止しました。"
    fi
  }
  trap cleanup_vm EXIT
fi

# VM を起動し、SSH が受け付けるまで必ず待つ。
# 作りたての VM は RUNNING でも sshd がまだ接続を拒否する ("Connection refused")
# ことがあるため、状態に関わらず SSH が通るまでポーリングする。
st=$(gcloud compute instances describe "$REMOTE" --project "$GCP_PROJECT" --zone "$GCP_ZONE" --format='value(status)' 2>/dev/null || echo UNKNOWN)
if [ "$st" != "RUNNING" ]; then
  echo "[info] VM を起動中... (現在: $st)"
  gcloud compute instances start "$REMOTE" --project "$GCP_PROJECT" --zone "$GCP_ZONE" >/dev/null
else
  echo "[info] VM は起動済みです。"
fi
echo "[info] SSH の準備を待機中..."
for _ in $(seq 1 30); do
  if gcloud compute ssh "$REMOTE" "${SSH_FLAGS[@]}" --command 'true' >/dev/null 2>&1; then break; fi
  sleep 5
done

echo "[info] VM ($GCP_VM / $GCP_ZONE) にツールを転送..."
run_ssh "mkdir -p $REMOTE_DIR/scripts"
# 実行に必要な最小ファイルだけ転送 (mcap 本体は転送しない)
for f in get_mcap_to_csv.py requirements.txt topics.example.t2.json topics.example.apollo.json; do
  gcloud compute scp "${SSH_FLAGS[@]}" "$REPO_DIR/$f" "$REMOTE:$REMOTE_DIR/$f"
done
gcloud compute scp "${SSH_FLAGS[@]}" "$REPO_DIR/scripts/run_on_gcp.sh" "$REMOTE:$REMOTE_DIR/scripts/run_on_gcp.sh"
# CRLF チェックアウト対策 (bash が pipefail\r 等で失敗するため VM 側で除去)
run_ssh "sed -i 's/\r//' $REMOTE_DIR/scripts/run_on_gcp.sh"

# 手元で指定した topics JSON を VM へ転送し、VM 上では basename で参照する
if [ -n "$TOPICS_FILE" ]; then
  [ -f "$TOPICS_FILE" ] || { echo "[error] topics JSON がありません: $TOPICS_FILE"; exit 1; }
  TNAME="$(basename "$TOPICS_FILE")"
  gcloud compute scp "${SSH_FLAGS[@]}" "$TOPICS_FILE" "$REMOTE:$REMOTE_DIR/$TNAME"
  set -- "$@" --topics "$TNAME"
fi

# ②選択のファイル一覧 JSON も同様に転送する (VM の再検索をスキップし選択と一致させる)
if [ -n "$GCS_FILES_FILE" ]; then
  [ -f "$GCS_FILES_FILE" ] || { echo "[error] gcs-files JSON がありません: $GCS_FILES_FILE"; exit 1; }
  GNAME="$(basename "$GCS_FILES_FILE")"
  gcloud compute scp "${SSH_FLAGS[@]}" "$GCS_FILES_FILE" "$REMOTE:$REMOTE_DIR/$GNAME"
  set -- "$@" --gcs-files "$GNAME"
fi

# /t2 デコード用の私物パッケージを手元から VM へ転送 (VM での GitHub 認証を回避)
ROS2IDL_REMOTE=""
if [ -n "${ROS2IDL_LOCAL_PATH:-}" ]; then
  if [ -d "$ROS2IDL_LOCAL_PATH" ]; then
    echo "[info] mcap-ros2idl-support を VM へ転送 (元: $ROS2IDL_LOCAL_PATH)"
    # ローカル側の apex_json 対応チェック (古い/別フォルダを指していると気づける)
    FAC_LOCAL=$(find "$ROS2IDL_LOCAL_PATH" -name decode_factory.py 2>/dev/null | head -1)
    if [ -n "$FAC_LOCAL" ] && grep -q 'apex_json' "$FAC_LOCAL"; then
      echo "[info] ローカルソースは apex_json 対応です。"
    else
      echo "[warn] このローカル mcap-ros2idl-support は apex_json 非対応 (古い) です: ${FAC_LOCAL:-decode_factory.py 見つからず}"
      echo "       そのリポジトリで git pull するか、gcp.env の ROS2IDL_LOCAL_PATH を修正してください。"
    fi
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

# 手元の ADC を VM にコピー (VM が自分の権限で GCS を読めるようにする。IAM 変更不要)
if [ "$SETUP_AUTH" = "1" ]; then
  ADC="${GOOGLE_APPLICATION_CREDENTIALS:-$HOME/.config/gcloud/application_default_credentials.json}"
  if [ ! -f "$ADC" ]; then
    echo "[error] ローカルの ADC が見つかりません: $ADC"
    echo "        先に 'gcloud auth application-default login' を実行してください。"
    exit 1
  fi
  echo "[info] ローカルの認証情報 (ADC) を VM へコピー..."
  run_ssh "mkdir -p .config/gcloud"
  gcloud compute scp "${SSH_FLAGS[@]}" "$ADC" "$REMOTE:.config/gcloud/application_default_credentials.json"
  echo "[ok] 以後この VM はあなたの権限で GCS を読みます。"
fi

# 一覧/確認/見積もりモードは CSV を出さないので、実行だけで終わる
NO_CSV_MODE=0
for a in "$@"; do
  case "$a" in
    --list-only | --list-topics | --estimate) NO_CSV_MODE=1 ;;
  esac
done

echo "[info] VM 上で抽出を実行 (GCS 読み込みは egress 無料)..."
run_ssh "cd $REMOTE_DIR && ${VENV_EXPORT}bash scripts/run_on_gcp.sh$ARGS"

if [ "$NO_CSV_MODE" = "1" ]; then
  echo "[ok] 完了 (一覧/見積もりモードのため CSV ダウンロードはありません)。"
  exit 0
fi

echo "[info] CSV を手元へダウンロード..."
mkdir -p "$LOCAL_OUT"
gcloud compute scp "${SSH_FLAGS[@]}" "$REMOTE:$REMOTE_DIR/out_csv.tar.gz" "$LOCAL_OUT/out_csv.tar.gz"
tar -xzf "$LOCAL_OUT/out_csv.tar.gz" -C "$LOCAL_OUT"
rm -f "$LOCAL_OUT/out_csv.tar.gz"

echo "[ok] 完了。CSV は $LOCAL_OUT/ に展開しました:"
ls -1 "$LOCAL_OUT"/*.csv 2>/dev/null || echo "  (CSV が見つかりません。VM 側のログを確認してください)"
