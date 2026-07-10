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
PYTHON="${PYTHON:-python3}"

# --- 依存関係の用意 (自己完結。VM 作成時のインストールに依存しない) ---
# VENV_DIR を明示指定した場合のみ venv を使う (既存 venv の流用向け)。
# 既定は使い捨て VM なので system python に直接入れる (python3-venv 不要)。
PIP=""
if [ -n "${VENV_DIR:-}" ]; then
  [ -d "$VENV_DIR" ] || "$PYTHON" -m venv "$VENV_DIR"
  # shellcheck disable=SC1090
  source "$VENV_DIR/bin/activate"
  PYTHON=python
  PIP="pip install --quiet"
else
  # system python に入れる。pip が無ければ apt で用意 (GCE は passwordless sudo)。
  if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
    echo "[info] installing python3-pip..."
    sudo apt-get update -qq && sudo apt-get install -y -qq python3-pip
  fi
  # Debian 12 は PEP668 (externally-managed) なので --break-system-packages を付ける
  PIP="$PYTHON -m pip install --quiet --break-system-packages"
fi

# requirements.txt は private git 依存と streamlit を含むため VM では使わず、
# CLI に必要なものだけを直接入れる (VM での GitHub 認証が不要になる)。
if ! "$PYTHON" -c "import mcap, mcap_protobuf, google.cloud.storage" 2>/dev/null; then
  echo "[info] installing dependencies..."
  $PIP "mcap>=1.3.0" "mcap-protobuf-support>=0.5.3" "google-cloud-storage>=2.14.0"
fi

# mcap-ros2idl-support (/t2/* デコード用) は ROS2IDL_PATH でローカルパスを渡すと入る。
if ! "$PYTHON" -c "import mcap_ros2idl_support" 2>/dev/null; then
  if [ -n "${ROS2IDL_PATH:-}" ] && [ -e "$ROS2IDL_PATH" ]; then
    echo "[info] installing mcap-ros2idl-support: $ROS2IDL_PATH"
    $PIP "$ROS2IDL_PATH"
  else
    echo "[warn] mcap-ros2idl-support not installed; /t2/* topics cannot be decoded."
    echo "       Set ROS2IDL_LOCAL_PATH in gcp.env to your zero-plotter/mcap-ros2idl-support."
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
# VM ではキャッシュを持たない (--no-cache)。egress 無料なので再読み込みしても課金されず、
# ディスクを小さく保てる (キャッシュでディスクが膨らむのを防ぐ)。
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
"$PYTHON" get_mcap_to_csv.py "$@" --outdir "$OUT_DIR" --no-cache

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
