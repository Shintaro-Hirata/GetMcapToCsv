# GetMcapToCsv

走行データの mcap 保管 GCS バケット `t2-ft-original-data` から、
**指定 vehicle ID・指定時間帯** のメッセージを抽出して CSV に変換するツール。

ローカルに保存済みの mcap ファイルの処理（`extract.py` 相当）にも対応している。

## 参考にした extract.py が動かなかった理由（おそらく）

`t2-ft-original-data` に保管されている mcap は **ROS2 (ros2idl) エンコードの
`/t2/*` トピック**が中心。参考の `extract.py` は

- protobuf デコーダ (`mcap-protobuf-support`) のみ使用
- `/apollo/canbus/chassis`, `/apollo/control` トピック前提

だったため、このデータでは対象トピックが 1 件もヒットせず「動かない」状態になる。
本ツールは ros2idl / protobuf 両方のデコーダに対応し、`--list-topics` で
実際に含まれるトピックとエンコーディングを確認できるようにしている。

## バケットの構造（前提）

```
t2-ft-original-data/
  20251204_GIGA07__xxxx/                     # YYYYMMDD_<vehicle>__...
    _EXPGRP_CLOUD_UPLOAD_COMPLETE
    recording/
      20251204_120023_GIGA07_xxxx_route/     # YYYYMMDD_HHMMSS_<vehicle>_...
        record_develop/
          record_develop_0.mcap
          record_develop_1.mcap
          ...
```

ツールは以下の手順で対象を絞り込むため、**mcap 全体をダウンロードせずに**
指定時間帯のデータだけを読み取る:

1. 日付 + vehicle ID でトップレベルディレクトリを列挙（前日分も含めて midnight 跨ぎに対応）
2. `recording/*/record_develop/*.mcap` を列挙し、セッション開始時刻で粗く絞り込み
3. 各 mcap の**サマリ（フッタ）だけ**を範囲読み取りし、メッセージ時刻範囲が重なるファイルに限定
4. mcap のチャンクインデックスを使って、指定時間帯のチャンクのみ GCS からストリーミング読み取り

## セットアップ

前提: Python 3.10 以上、[gcloud CLI](https://cloud.google.com/sdk/docs/install)、git

```bash
pip install -r requirements.txt

# GCP 認証（初回のみ。バケット t2-ft-original-data の読み取り権限が必要）
gcloud auth application-default login
```

Windows の場合は `install_deps.bat` をダブルクリックでも可。

### mcap-ros2idl-support のインストール

`/t2/*` トピック（ros2idl）のデコードには社内パッケージ
[`mcap-ros2idl-support`](https://github.com/t2-auto/zero-plotter/tree/main/mcap-ros2idl-support)
が必要。`requirements.txt` に git 経由のインストールを含めているが、
private リポジトリのため GitHub 認証で失敗する場合は、手動で:

```bash
git clone https://github.com/t2-auto/zero-plotter.git
pip install ./zero-plotter/mcap-ros2idl-support
```

## 使い方

### 1. 対象ファイルの確認（ダウンロードなし・軽い）

```bash
python get_mcap_to_csv.py --vehicle GIGA07 --start "2025-12-04 12:00" --end "2025-12-04 12:10" --list-only
```

### 2. 含まれるトピックの確認（トピック名・フィールド調査用）

```bash
python get_mcap_to_csv.py --vehicle GIGA07 --start "2025-12-04 12:00" --end "2025-12-04 12:10" --list-topics
```

### 3-a. CSV 抽出 — とりあえず全トピック欲しい場合（設定ファイル不要）

`--all-topics` を付けると、mcap に含まれる全トピックを 1 トピック 1 CSV で出力する。
トピック名やフィールド名を調べたり設定 JSON を書く必要はない。

```bash
# ローカル mcap の全トピックを CSV 化
python get_mcap_to_csv.py --local "録画ファイル.mcap" --all-topics --outdir out

# GCS から全トピック
python get_mcap_to_csv.py --vehicle GIGA07 --start "2025-12-04 12:00" --end "2025-12-04 12:10" \
    --all-topics --outdir out
```

出力は `<base>_<トピック名>.csv`（トピック名の `/` は `_` に変換）。
トピック数が多いと CSV も多数（例: 80 本前後）生成され、
列数が膨大になる結合 CSV (`_all.csv`) はこのモードでは作らない。

### 3-b. CSV 抽出 — トピック・フィールドを絞る場合

必要なトピックと値だけ欲しいときは設定 JSON を指定する（`_all.csv` も生成される）。

```bash
python get_mcap_to_csv.py --vehicle GIGA07 --start "2025-12-04 12:00" --end "2025-12-04 12:10" \
    --topics topics.example.t2.json --outdir out
```

出力（`out/` 以下）:

- `GIGA07_20251204_120000-121000_<suffix>.csv` … トピックごとの CSV（歯抜けなし）
- `GIGA07_20251204_120000-121000_all.csv` … 全トピックを時刻順に 1 本へ結合（値のない列は空欄）

各 CSV の先頭列は `extract.py` と同じ:

| 列 | 内容 |
|----|------|
| `time_jst` | `MM/DD HH:MM:SS.mmm`（日本時間） |
| `t_sec` | 抽出範囲先頭からの経過秒 |
| `t_ns` | 元のタイムスタンプ（ナノ秒, epoch） |

### 4. ローカル mcap の処理（GCS を使わない）

```bash
# Apollo 系 (protobuf) の mcap
python get_mcap_to_csv.py --local "*.mcap" --topics topics.example.apollo.json

# 時間帯で絞ることも可能
python get_mcap_to_csv.py --local "C:\data\*.mcap" --start "2025-12-04 12:00" --end "2025-12-04 12:10"
```

## トピック設定 JSON

```json
{
  "/t2/localization_compositor/pose": {
    "suffix": "pose",
    "fields": ["pose.position.x", "pose.position.y"]
  },
  "/t2/control/debug": {
    "suffix": "control",
    "fields": []
  }
}
```

- `suffix` … 出力 CSV のファイル名接尾辞（省略時はトピック名から自動生成）
- `fields` … 取り出すフィールド（ドット区切り、配列は `foo[0].bar` 形式も可）。
  **空リスト `[]` にするとメッセージ全体をフラット展開して全列出力**する。
  フィールド名が分からないうちは `[]` で出力し、列名を見てから絞り込むのが楽。
- `_` で始まるキー（`_comment` など）は無視される。

サンプル: [`topics.example.t2.json`](topics.example.t2.json)（バケットの /t2 データ用）、
[`topics.example.apollo.json`](topics.example.apollo.json)（extract.py と同じ /apollo 用）

## 主なオプション

| オプション | 説明 |
|-----------|------|
| `--vehicle` | vehicle ID（例: `GIGA07`） |
| `--start` / `--end` | 抽出時間帯（JST）。`"2025-12-04 12:00"` / `"2025/12/04 12:00:00"` など |
| `--all-topics` | 全トピックを抽出（設定 JSON 不要、1 トピック 1 CSV） |
| `--topics` | トピック設定 JSON（省略時は /apollo 用デフォルト） |
| `--outdir` | CSV 出力先（デフォルト `out`） |
| `--bucket` | バケット名（デフォルト `t2-ft-original-data`） |
| `--subdir` | セッション内の対象サブディレクトリ（デフォルト `record_develop`、`"*"` で全て） |
| `--list-only` | 対象 mcap の一覧表示のみ |
| `--list-topics` | トピック一覧表示（メッセージ数・エンコーディング・スキーマ名） |
| `--local` | ローカル mcap を処理（glob パターン可） |

## トラブルシューティング

- **`対象トピックのメッセージが 1 件も見つかりませんでした`**
  → `--list-topics` で実際のトピック名を確認し、設定 JSON を修正する。
- **`mcap-ros2idl-support が見つかりません` の警告が出る**
  → 上記「mcap-ros2idl-support のインストール」を実施。これがないと `/t2/*` は空になる。
- **認証エラー**
  → `gcloud auth application-default login` を実行。バケットの読み取り権限が
  自分のアカウントに付与されているかも確認する。
- **遅い**
  → 抽出時間帯を短くする、`--topics` のトピック数を減らす。時間帯指定により
  必要なチャンクのみ読むため、時間帯が短いほど速い。
