# GCP 内実行で GCS ダウンロード課金を下げる

## なぜこれが効くのか

GCS の課金（egress）は「**バケットからインターネット側へ出たバイト数**」に対して発生する。
mcap は全トピックが混在した zstd 圧縮チャンクで格納されているため、特定トピック・
特定カラムだけ欲しくても **mcap 本体を丸ごとダウンロードするしかない**（クライアント側で
転送量は減らせない。`--estimate` で削減率がほぼ 0% になるのはこのため）。

唯一の根本策は、**mcap → CSV の変換を GCP の中（バケットと同一リージョンの VM）で済ませ、
外に出るのを軽い CSV だけにする**こと。GCP 内（同一リージョン）の GCS 読み込みは egress
課金がかからないので、ネットワーク課金を **95〜99% 削減**できる。

```
 従来:  GCS ──[ mcap 1.4GB / ¥25 ]──> 手元PC ──変換──> CSV
 GCP内: GCS ──[ mcap 1.4GB / ¥0 ]──> VM(同一リージョン) ──変換──> CSV ──[ 数MB ]──> 手元PC
```

## 前提と準備

- **バケットと同一リージョンの VM**。`t2-ft-original-data` を扱う zero-plotter の VM が
  あればそれを流用できる（無ければ小さな VM を1台用意）。リージョン確認:
  ```bash
  gcloud storage buckets describe gs://t2-ft-original-data --format="value(location)"
  ```
  この location と同じリージョンに VM を置く（例 `asia-northeast1`）。
- VM のサービスアカウントに **バケットの読み取り権限**（`roles/storage.objectViewer`）。
- VM に **Python 3.10+** と、`/t2/*` デコード用の **mcap-ros2idl-support**。
  zero-plotter の venv を流用するのが早い（`VENV_DIR` で指定可）。無ければ:
  ```bash
  git clone https://github.com/t2-auto/zero-plotter.git
  pip install ./zero-plotter/mcap-ros2idl-support
  ```
- 手元 PC に **gcloud CLI**（認証済み）。

## 使い方 A: 手元 PC から一発（推奨）

`scripts/gcp_fetch.sh` が「ツール転送 → VM で変換 → CSV だけ回収」を自動でやる。
手元の操作感は今までと同じで、重い処理だけ VM に逃がす。

```bash
# 1. 設定を用意（初回のみ）
cp scripts/gcp.env.example scripts/gcp.env
#   → GCP_PROJECT / GCP_ZONE / GCP_VM を自分の VM に合わせて編集
#   （既存 venv を使うなら VENV_DIR も）

# 2. 実行（get_mcap_to_csv.py と同じ引数がそのまま使える）
bash scripts/gcp_fetch.sh --vehicle GIGA09 --start "2026-07-01 20:40" \
    --end "2026-07-01 20:45" --topics topics.example.t2.json
```

完了すると手元の `out/` に CSV が展開される。GCS からの重い読み込みは VM 内で完結し、
インターネットを渡るのは CSV（数MB）だけ。

> Windows の場合は Git Bash か WSL でこのスクリプトを実行するのが簡単。
> PowerShell だけで済ませたい場合は「使い方 C」の gcloud コマンドを手打ちする。

## 使い方 B: VM に入って直接

VM に SSH して直接動かす場合:

```bash
gcloud compute ssh <VM> --zone <ZONE> --project <PROJECT>
# --- VM 上 ---
cd ~/GetMcapToCsv                       # ツール一式を置いた場所
bash scripts/run_on_gcp.sh --vehicle GIGA09 --start "2026-07-01 20:40" \
    --end "2026-07-01 20:45" --topics topics.example.t2.json
# → out/*.csv と out_csv.tar.gz が生成される
```

生成された CSV を手元へ:
```bash
gcloud compute scp <VM>:~/GetMcapToCsv/out_csv.tar.gz . --zone <ZONE> --project <PROJECT>
tar -xzf out_csv.tar.gz -C out
```

## 使い方 C: gcloud を手で叩く（PowerShell 等）

```powershell
# 変換を VM 上で実行
gcloud compute ssh VM --zone ZONE --project PROJECT --command "cd ~/GetMcapToCsv && bash scripts/run_on_gcp.sh --vehicle GIGA09 --start '2026-07-01 20:40' --end '2026-07-01 20:45' --topics topics.example.t2.json"
# CSV を回収
gcloud compute scp VM:~/GetMcapToCsv/out_csv.tar.gz . --zone ZONE --project PROJECT
```

## コストの目安と損益分岐

| 項目 | 従来（手元DL） | GCP 内実行 |
|------|----------------|------------|
| mcap の egress | 1回 1.4GB ≒ ¥25（imageも含めれば数百円） | ¥0（同一リージョン） |
| CSV の egress | ― | 数MB ≒ ¥0 |
| VM 費用 | ― | 常時起動なら月数千円。オンデマンド起動なら1回あたり数円〜十数円 |

- **既存の zero-plotter VM を間借りできるなら追加の VM 費用ゼロ**で、ほぼ純粋に節約になる。
- 専用 VM を用意する場合は、使う時だけ起動（`gcloud compute instances start/stop`）すれば
  VM 費用も最小化できる。抽出が「それなりの回数・広い時間帯」なら十分ペイする。

## 注意

- CSV 化する内容（トピック・カラム）を絞るほど回収する CSV が小さくなる。転送量削減には
  効かないが、GCP 内実行では **回収する CSV のサイズ = 実質的な egress** になるので、
  ここで絞るとそのまま課金削減につながる（クライアント直 DL とは意味が逆転して効く）。
- VM に mcap-ros2idl-support が入っていないと `/t2/*` が空になる。実行ログの
  `[warn] mcap-ros2idl-support が未インストール` に注意。
