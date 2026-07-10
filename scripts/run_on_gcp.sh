#!/usr/bin/env bash
# GCP の VM 上で実行するラッパ。
# ここで mcap -> CSV 変換まで済ませ、外に出るのは軽い CSV だけにする
# （GCS からの読み込みは同一リージョンの VM 内なら egress 課金なし）。
#
# 使い方 (VM 上):
#   bash scripts/run_on_gcp.sh --vehicle GIGA09 --start "2026-07-01 20:40" \
#       --end "2026-07-01 20:45" --topics topics.example.t2.json
#
# get_mcap_to_csv.py への引数はすべてそのまま渡る (--all-topics / --exclude-topics 等も可)。
# --outdir は指定不要 (out/ に固定し、末尾で tar にまとめる)。
set -euo pipefail

cd "$(dirname "$0")/.."   # リポジトリルート

OUT_DIR="${OUT_DIR:-out}"
VENV_DIR="${VENV_DIR:-.venv}"
PYTHON="${PYTHON:-python3}"

# --- 依存関係の用意 (既存 venv を VENV_DIR で指定すれば再利用) ---
if [ ! -d "$VENV_DIR" ]; then
  echo "[info] venv を作成: $VENV_DIR"
  "$PYTHON" -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

# requirements.txt は private git 依存 (mcap-ros2idl-support) と streamlit を含むため
# VM では使わず、CLI に必要なものだけを直接入れる (VM での GitHub 認証が不要になる)。
if ! python -c "import mcap, mcap_protobuf, google.cloud.storage" 2>/dev/null; then
  echo "[info] 依存関係をインストール中..."
  pip install --quiet "mcap>=1.3.0" "mcap-protobuf-support>=0.5.3" "google-cloud-storage>=2.14.0"
fi

# mcap-ros2idl-support (/t2/* デコード用) は ROS2IDL_PATH でローカルパスを渡すと入る。
# gcp_fetch.sh が手元の zero-plotter/mcap-ros2idl-support を転送してここに渡す。
if ! python -c "import mcap_ros2idl_support" 2>/dev/null; then
  if [ -n "${ROS2IDL_PATH:-}" ] && [ -e "$ROS2IDL_PATH" ]; then
    echo "[info] mcap-ros2idl-support をインストール: $ROS2IDL_PATH"
    pip install --quiet "$ROS2IDL_PATH"
  else
    echo "[warn] mcap-ros2idl-support が未インストールです。/t2/* トピックはデコードできません。"
    echo "       gcp.env の ROS2IDL_LOCAL_PATH に手元の zero-plotter/mcap-ros2idl-support を指定してください。"
  fi
fi

# 一覧・トピック確認・見積もりモードは CSV を出さない (成功扱いで抜ける)
NO_CSV=0
for a in "$@"; do
  case "$a" in
    --list-only | --list-topics | --estimate) NO_CSV=1 ;;
  esac
done

# --- 抽出 (GCS 読み込みは VM 内なので egress 無料) ---
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
python get_mcap_to_csv.py "$@" --outdir "$OUT_DIR"

if [ "$NO_CSV" = "1" ]; then
  echo "[ok] 一覧/見積もりモードのため CSV 出力はありません。"
  exit 0
fi

# --- CSV を 1 つの tar.gz にまとめる (取り出しを scp 1 回で済ませる) ---
ARCHIVE="${ARCHIVE:-out_csv.tar.gz}"
if compgen -G "$OUT_DIR/*.csv" > /dev/null; then
  tar -czf "$ARCHIVE" -C "$OUT_DIR" .
  echo "[ok] CSV をまとめました: $(pwd)/$ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
else
  echo "[warn] CSV が生成されませんでした。引数・トピック指定を確認してください。"
  exit 1
fi
