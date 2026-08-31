# GetMcapToCsv

走行データの mcap 保管 GCS バケット `t2-ft-original-data` から、
**指定 vehicle ID・指定時間帯** のメッセージを抽出して CSV に変換するツール。

ローカルに保存済みの mcap ファイルの処理（`extract.py` 相当）にも対応している。

> **初めて使う人へ**: セットアップから CSV 取得までを時系列で追える
> [利用マニュアル (docs/MANUAL.md)](docs/MANUAL.md) を先に読むのがおすすめ。
> この README は CLI オプション等のリファレンス寄りの内容。

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

## ブラウザ UI（おすすめ）

`start_ui.bat` をダブルクリック（またはターミナルで `streamlit run app.py`）すると
ブラウザで操作できる UI が起動する。

1. **① 検索条件** — 車両ID・開始/終了の日付と時刻を入力して「候補ファイルを検索」
   （日付をまたぐ指定も可）。
   `record_debug_image` は既定で含める（ほぼ毎回使うため）、`record_sensor` はチェックで追加
2. **② 対象ファイル** — 見つかった mcap を 1 行 1 ファイルの表で確認
   （種類 develop/image/sensor・時間帯・サイズ付き）。チェックで選択・除外
3. **③ トピック選択** — 「トピック一覧を取得」でデータに含まれる実トピックを取得し、
   1 行 1 トピックの表から選択。`mcap_presets.json` のプリセット（切り出し君互換 等）で
   まとめ選択も可。「カラム絞り込み」でトピック内の**列単位の選択**もできる
   （カラム一覧を取得すると全選択状態で表示され、不要な列を外せる。CSV 出力のみ有効。
   列を絞っても GCS 転送量は変わらない点に注意）
4. **④ 出力** — 出力形式と出力フォルダを指定して「抽出実行」
   - **CSV（トピック別）** … 従来どおりのCSV出力
   - **mcap（時間帯クロップ + トピック絞り込み）** … 指定時間帯・指定トピックだけを
     1 本の mcap に生データのままコピー（デコードしないので速い）
   - **mcap（元ファイルをそのまま保存）** … 対象の 1 分刻み mcap をそのままダウンロード

①〜⑤で入れた条件・選択はページ最上部の「💾 設定の保存・読み込み」で保存/復元できる。
名前を付けて「この PC に保存」すれば次回は一覧から選ぶだけ（共有用に JSON ファイルとして
の保存/読み込みも可）。読み込み時は既定で**車両ID・取得日付・時刻をファイルから復元せず
①の入力を使う**ので、「同じトピック構成で別の車両・別の日を抽出」が ①に入力 → 設定を適用、
だけで済む（適用後は検索〜トピック一覧の取得まで自動実行され、あとは「抽出実行」を押すだけ。
保存時の車両・日付ごと再現したいときはチェックを外す）。

### CSV の抽出ルート「VM 経由（課金最小・推奨）」

④ 出力で CSV を選ぶと「**CSV の抽出ルート**」が出る。**VM 経由**を選ぶと、
①〜③で決めた時間帯・トピック・カラム絞り込みをそのまま GCP 内の VM に渡し、
VM 作成 → VM 上で mcap→CSV 変換 → CSV だけ回収 →（モデルB なら）VM 削除、までを
1 ボタンで行い、ログを画面に流す。**mcap は GCP から出ないので egress 課金がほぼ 0**
（実行前に「この PC で直接抽出した場合の課金＝節約見込み」も表示される）。
事前に `scripts/gcp.env` の設定と `gcloud auth application-default login` が必要
（詳細は [docs/GCP_EXECUTION.md](docs/GCP_EXECUTION.md)）。
「課金状況を確認」ボタンで残 VM の点検ができ、ネットワーク断などで VM が残って
しまった場合は表示される「残っている VM を削除する」ボタンで**その場で削除**できる
（VM 経由を開いたときにも残存を自動点検して警告する）。さらに保険として、VM は起動した
まま一定時間（既定 12 時間、`gcp.env` の `MAX_RUN_HOURS`）たつと GCP 側が自動削除する。

プリセットは `mcap_presets.json` を編集すれば増やせる。
「切り出し君互換」プリセットは仮のリストなので、実際の切り出し君の対象トピックに
合わせて更新すること。

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

周波数が異なるトピックを 1 本にまとめると `_all.csv` は歯抜けになるため、
解析・図示用には `--merged-grid 0.1` で**共通時間軸に揃えた結合 CSV**
（`..._all_100ms.csv`）を出力できる。一定周期の時刻グリッドの各セルに
「その時刻までの最新値」（前値ホールド）が入るので、そのままグラフ化できる。
列名は `トピックsuffix.列名`。`--merged-hold`（既定 5 秒）を超えて更新が無い
区間は空欄になり、実際のデータ欠損が見える。UI では ④ の「結合 CSV の形式」で選ぶ。

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
- `columns` …（任意）フラット展開時に**残す列をフル列名で指定**する
  （例 `["position.x", "speed"]`）。`fields` との違い: `fields` は列名が最後の要素名に
  短縮される extract.py 互換形式、`columns` はフラット展開の列名そのまま。
  UI のカラム絞り込みはこちらを使う。
- `rename` …（任意）CSV に出力する**列名の変更** `{元の列名: 出力名}`
  （例 `{"speed": "車速[km/h]"}`）。トピック別 CSV と結合 CSV（時間軸そろえ）の
  ヘッダに反映される。客先向けの分かりやすい名前を付けたいときに使う。
  UI では ③ の「カラム絞り込み・列名変更」から設定できる。
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
| `--merged-grid` | 結合 CSV を一定周期（秒）の共通時間軸に揃えて出力（例: `0.1`）。各セルはその時刻までの最新値（前値ホールド） |
| `--merged-hold` | `--merged-grid` で前値を保持する最大秒数。超えた区間は空欄（0=無制限、デフォルト 5） |
| `--split-minutes` | 指定した分数ごとに出力ファイルを区切る（例: `30`）。読み込みは 1 回のまま書き出しだけ分割 |
| `--bucket` | バケット名（デフォルト `t2-ft-original-data`） |
| `--subdir` | セッション内の対象サブディレクトリ（デフォルト `record_develop`、`"*"` で全て） |
| `--workers` | 時刻メタデータ読み込みの並列数（デフォルト 16） |
| `--session-lookback` | 抽出開始の何時間前までに始まったセッションを候補に含めるか（デフォルト 24） |
| `--extract-workers` | 抽出（ダウンロード + デコード）のファイル並列数（デフォルトは自動） |
| `--exclude-topics` | 除外するトピックのパターン（ワイルドカード可、複数指定可） |
| `--no-download` | 一括ダウンロードを禁止し常にチャンク単位の部分読み込みにする |
| `--include-sensor` | develop で確定した連番と同じ `record_sensor` の mcap も抽出対象に加える |
| `--sensor-subdir` | `--include-sensor` で追加するサブディレクトリ名（デフォルト `record_sensor`） |
| `--include-image` | develop で確定した連番と同じ `record_debug_image` の mcap も抽出対象に加える |
| `--image-subdir` | `--include-image` で追加するサブディレクトリ名（デフォルト `record_debug_image`） |
| `--list-only` | 対象 mcap の一覧表示のみ |
| `--list-topics` | トピック一覧表示（メッセージ数・エンコーディング・スキーマ名） |
| `--local` | ローカル mcap を処理（glob パターン可） |

## GCS 課金とその節約

GCS からのダウンロードには egress 課金（東京→インターネットで約 $0.12/GB ≒ 18円/GB）が
かかる。**手動ダウンロード（ブラウザ/gsutil/Console）でも料金は同じ**で、
ツールを介すかどうかは無関係（課金は「GCS から外に出たバイト数」に対して発生する）。

本ツールの節約機能:

- **ローカルキャッシュ（既定で有効）**: 一括ダウンロードした mcap を `mcap_cache/` に保存し、
  同じファイルへの再アクセス時は GCS を読まない（再課金ゼロ・オフラインでも動く）。
  上限サイズ（既定 20GB）を超えると古いものから自動削除。
  CLI: `--cache-dir` / `--no-cache` / `--cache-max-gb`。UI: 詳細オプション内
  （現在サイズの確認・クリアも可能）
- **転送量の見える化**: 実行のたびに「GCS 読み込み量と概算費用、キャッシュによる節約分」を表示
- **ローカル mcap モード**: ダウンロード済みの mcap から直接 CSV 抽出（GCS 課金なし）。
  CLI は `--local`、UI は ① の「入力元」で「ローカルの mcap」を選択してフォルダを指定
- CSV 抽出だけが目的なら `record_debug_image` を外すと転送量を大きく削減できる
- **サマリ事前判定（自動）**: 抽出時にファイルごとのサマリを先に読み、選択トピックの
  メッセージが指定時間帯に 1 件も無いファイルは**ダウンロードせずスキップ**する
  （実体が develop/sensor の片側にしか無いトピックで GB 級の読み込みを省く）
- **トピック選択によるチャンクスキップ（自動）**: mcap はチャンク（複数トピック混在の
  圧縮ブロック）単位でしか取得できないため、トピックを絞っても削減できるとは限らないが、
  選択トピックを含まないチャンクはスキップできる。抽出時にサマリから必要チャンク比を
  自動判定し、**40% 以上削減できるファイルは丸ごとダウンロードをやめて部分読み込みに
  切り替える**（それ未満は従来どおり一括ダウンロード + キャッシュ）
- **転送量の事前見積もり**: 実際にどれだけ減るかはデータのチャンク構成に依存するため、
  実測ベースの見積もり機能がある。CLI は `--estimate`、UI は ④ の
  「転送量を見積もる」ボタン（読むのはサマリ数MBのみ）。
  出力には「チャンクスキップでの削減率」「選択トピックの実データ比率」「圧縮方式」が
  含まれ、圧縮方式が非圧縮ならメッセージ単位取得（未実装）でさらに削減できる余地が分かる。
  大幅削減が構造的に不可能な場合の根本策は GCP 内（同リージョン VM）での実行
  （egress 無料。zero-plotter の VM 上で CLI を動かし、小さな CSV だけ手元へ）。
  → 手順とスクリプト: [docs/GCP_EXECUTION.md](docs/GCP_EXECUTION.md)。
  Windows PowerShell は `scripts/gcp_create_vm.ps1` / `scripts/gcp_fetch.ps1`、
  Git Bash / WSL / macOS / Linux は同名の `.sh`（手元 PC から一発で VM 変換 → CSV 回収）

単価は `get_mcap_to_csv.py` 先頭の `EGRESS_USD_PER_GB` / `USD_JPY` で調整可能（表示のみに使用）。

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
