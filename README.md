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
  20260702_GIGA09__SSD0097-FOT/              # 運行終了日_車両名__SSD名
    _EXPGRP_CLOUD_UPLOAD_COMPLETE
    recording/
      20260701_190845_GIGA09_d079c0b2/       # 録画開始時刻_車両名_... (セッション)
        record_develop/
          record_develop_0.mcap
          record_develop_1.mcap
          ...
        record_sensor/                       # develop と同時刻で分割された sensor 系 mcap
          record_sensor_0.mcap
          ...
```

トップレベルの日付は**運行終了日**なので、夜間の走行データは翌日付の
ディレクトリに入ることに注意（例: 7/1 19:08 開始の走行 → `20260702_...` 配下）。

ツールは以下の手順で対象を絞り込むため、**mcap 全体をダウンロードせずに**
指定時間帯のデータだけを読み取る:

1. 日付 + vehicle ID でトップレベルディレクトリを列挙。日付は運行終了日のため、
   抽出開始日〜抽出終了日+1日を候補にする
2. `recording/` 直下の**セッションディレクトリ名**（`20260701_190845_...` = 録画開始時刻）
   だけを一覧し、時間帯に関係するセッションのみ採用。
   採用したセッションの `record_develop/` だけをオブジェクト listing する
   （時間帯外のセッションや他サブディレクトリは一覧すら取得しない）。
   採用条件: 抽出終了以前かつ抽出開始の `--session-lookback` 時間（既定 24h）以内に開始
3. 各 mcap の**サマリ（フッタ）だけ**を範囲読み取りし、メッセージ時刻範囲が重なるファイルに限定。
   セッション内の連番ファイル（`record_develop_0,1,2,...`）は時刻が連続していることを利用し、
   **二分探索で境界のファイルのみ**読む。セッション間は並列（`--workers`, 既定 16）で処理
4. 対象ファイルを**ファイル単位の並列プロセス**（`--extract-workers`, 既定は CPU コア数から自動）で
   ダウンロード + デコードする。時間帯がファイルの大半を覆う場合は一括ダウンロード、
   一部だけの場合は mcap のチャンクインデックスで必要チャンクのみ部分読み取り

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

### 3-c. record_sensor も一緒に抽出する

`--include-sensor` を付けると、record_develop 側の絞り込みで確定した連番
（例: `record_develop_34〜39` → `record_sensor_34〜39`）をそのまま
`record_sensor/` にも適用して抽出対象に加える。develop と sensor は同時刻で
分割されているため、sensor 側の時刻メタデータ読み込みは行わない。

```bash
python get_mcap_to_csv.py --vehicle GIGA09 --start "2026-07-01 20:40" --end "2026-07-01 20:45" \
    --all-topics --include-sensor
```

sensor 側の mcap はサイズが大きいことが多いので、処理時間・メモリに注意。
sensor のトピックだけ欲しい場合は `--subdir record_sensor` で develop の代わりに
sensor を絞り込み対象にする方法もある。

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
| `--workers` | 時刻メタデータ読み込みの並列数（デフォルト 16） |
| `--session-lookback` | 抽出開始の何時間前までに始まったセッションを候補に含めるか（デフォルト 24） |
| `--extract-workers` | 抽出（ダウンロード + デコード）のファイル並列数（デフォルトは自動） |
| `--exclude-topics` | 除外するトピックのパターン（ワイルドカード可、複数指定可） |
| `--no-download` | 一括ダウンロードを禁止し常にチャンク単位の部分読み込みにする |
| `--include-sensor` | develop で確定した連番と同じ `record_sensor` の mcap も抽出対象に加える |
| `--sensor-subdir` | `--include-sensor` で追加するサブディレクトリ名（デフォルト `record_sensor`） |
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
  → 抽出はファイル単位で並列実行される。それでも遅い場合の打ち手:
  - **不要な重量級トピックを除外する**のが最も効く。生パケットや TF は行数が桁違いに多い:
    ```bash
    --exclude-topics "/t2/positioning_driver/internal/*" "/tf*" "/events/*"
    ```
  - 抽出時間帯を短くする、`--topics` で必要なトピックだけに絞る
  - `--extract-workers` を CPU コア数まで上げる（メモリ使用量も増える）
