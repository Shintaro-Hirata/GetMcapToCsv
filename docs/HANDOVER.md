# 引き継ぎドキュメント（新セッション向け）

旧 Claude Code セッションからの引き継ぎ資料。このリポジトリの目的・設計判断・
解決済みの落とし穴・未了事項をまとめる。**新しいセッションを始めるときは、
このファイルと README.md / docs/GCP_EXECUTION.md を読ませれば文脈が復元できる。**

- 開発ブランチ: `claude/mcap-csv-extraction-jlkg6d`（全作業がここに入っている）
- 関連リポジトリ: `Shintaro-Hirata/GetDruidUser`（同名ブランチ。取得した CSV の取り込み先）
- 利用者: hirata.s@t2.auto（Windows / PowerShell 5.1 / VS Code。VM やインフラの専門家ではない
  前提で、エラーメッセージと手順は具体的に書く方針）

## 1. このツールの目的と全体像

GCS バケット `t2-ft-original-data`（東京 asia-northeast1）にある走行データ mcap から、
車両ID・時間帯・トピック・カラムを指定して CSV を抽出する。抽出した CSV は
GetDruidUser に取り込んで BigQuery 欠損の穴埋め・統計比較に使う。

構成:
- `get_mcap_to_csv.py` — CLI 本体（検索・時刻絞り込み・抽出・CSV/mcap 出力・見積もり・キャッシュ）
- `app.py` — Streamlit UI（検索 → ファイル選択 → トピック/カラム選択 → 出力。
  CSV は「この PC で直接」と「VM 経由（課金最小）」の 2 ルート）
- `scripts/` — GCP 内実行（VM）用。`.ps1`（Windows 用・**ASCII のみ**）と `.sh` の両方を常に同時整備
- `docs/GCP_EXECUTION.md` — VM 運用の完全な手順書（コスト表・トラブルシュート込み）

## 2. コストの考え方（結論のみ）

- GCS→PC のダウンロードは egress 課金 約 $0.12/GB（≈¥18/GB）。**トピック/カラムを
  絞ってもダウンロード量は減らない**（zstd 圧縮チャンクに全トピックが混在。実測で
  チャンクスキップ削減 0.9%、実データ比率 18.6%）。
- よって CSV 取得の本命は **VM 経由**: バケットと同一リージョンの VM 内で mcap→CSV 変換
  すれば GCS 読み込みは無料。外に出るのは CSV（数百 KB〜数十 MB ≈ ¥1 前後）だけ。
- VM は **モデル B（毎回作って消す）** を採用。待機費 ¥0。1回あたり ¥5〜10。
  `SPOT=1`（gcp.env）で稼働費約 1/3（稀に中断→再実行すればよい。VM は自動削除）。
- 既定マシンは e2-standard-8（抽出並列 7。ツールの並列上限が 8 なのでこれ以上は不要）。
- 発生しうる課金の全リストと点検方法（`gcp_status`）は GCP_EXECUTION.md にある。

## 3. データ構造の前提（重要）

- GCS パス: `YYYYMMDD_<車両>__<SSD>/recording/<セッション>/record_develop|record_sensor|record_debug_image/record_*_N.mcap`
- **ディレクトリ名の日付は運行「終了」日**。検索は開始日〜終了日+1日を見る。
- develop / sensor / image は同時刻で連番分割された兄弟ファイル。**入っているトピックが違う**
  （例: eps001 系は sensor 側。develop にはトピック名だけ登録され中身が無いことがある）。
- エンコード: /t2/* は ros2idl、/t2/main_mabx/* と /t2/control/demand* は **apex_json**、
  /events/* は ros2msg、/apollo/* は protobuf。apex_json は
  **新しめの mcap-ros2idl-support（zero-plotter リポジトリ内）でないとデコードできない**。

## 4. 解決済みの落とし穴（再発したらここを見る）

| 症状 | 原因と対策（対策済み） |
|------|------------------------|
| .ps1 が日本語コメントで構文エラー | PowerShell 5.1 は UTF-8(BOMなし) を SJIS 解釈。**.ps1 は ASCII のみ**で書く |
| VM 上で `pipefail\r` エラー | Windows checkout の CRLF。転送後に `sed -i 's/\r//'`＋`.gitattributes` |
| pscp が `~` を展開できない | REMOTE_DIR はホーム相対（`GetMcapToCsv`）にする |
| `scp --recurse` がフォルダ転送失敗 | tar 固めて 1 ファイル転送→展開（Send-Folder） |
| VM から GCS 403 | VM の SA に権限なし → `-SetupAuth` で手元の ADC を VM へコピー |
| 初回 SSH `Connection refused` | VM は RUNNING でも sshd 未起動。SSH が通るまで必ずポーリング |
| SSH 待ちが 1 回で例外落ち | PS5.1 の `$ErrorActionPreference='Stop'` は外部コマンドの stderr で即死。`'Continue'`＋`$LASTEXITCODE` 判定 |
| apex_json トピックだけ 0 行 | ①手元 zero-plotter が古い（`git pull`）②古い `build/lib` を setuptools が拾う → **pip ビルドせず、転送ソースを PYTHONPATH で直接使う方式に変更済み**（run_on_gcp.sh） |
| 適用したのに 0 行・時刻不一致系 | BigQuery/pandas の **datetime64[us] と [ns] の混在**。`dt.as_unit("ns")` で必ず ns に揃える |
| Spot 中断 | `Remote side unexpectedly closed` → 再実行するだけ。UI が案内を出す |
| VM 作成が `Reauthentication failed. cannot prompt during non-interactive execution` | gcloud ログインの期限切れ（組織のセッションポリシー）。UI 経由だと端末が無く再認証プロンプトを出せない → app.py の `ensure_gcloud_auth` が実行前にトークンの生死を確認し、切れていれば新しいコンソールで `gcloud auth login` を自動起動して完了を待つ（CLI 認証と ADC は別々に期限が切れる点に注意） |

## 5. 未了事項・次のタスク候補

1. **専用 GCP プロジェクト**: 現在は t2-integration に相乗り。分離したいが、プロジェクト作成は
   組織権限が必要なため **GCP 管理者への依頼待ち**（手順は GCP_EXECUTION.md「専用プロジェクト」節）。
   作成後は gcp.env の GCP_PROJECT を書き換えるだけ。
2. **「切り出し君互換」プリセット**: `mcap_presets.json` のリストは仮。実物の対象トピックに
   合わせて更新が必要（ユーザーがリストを持ってくる）。
3. **配布準備**: ユーザーは社内配布を計画。README のセットアップ手順は一通りあるが、
   配布パッケージ化（zip 等）や gcloud 未導入者向け手順は未整備。
4. UI 実行中の操作でストリーム処理が中断されるのは Streamlit の仕様（注意書きで対応済み。
   根本対応するなら subprocess をバックグラウンド化して進捗をポーリングする作りに変える）。

## 6. ローカル環境（git 外）に依存するもの

プラン移行ではリポジトリと下記ローカル要素があれば継続できる（PC 側に残るため作業不要）:
- `scripts/gcp.env`（gitignore 済み。GCP_PROJECT=t2-integration / ZONE=asia-northeast1-a /
  VM=mcap-csv-worker / ROS2IDL_LOCAL_PATH=zero-plotter の mcap-ros2idl-support への絶対パス）
- gcloud CLI 認証と ADC（`gcloud auth application-default login` 済み）
- zero-plotter クローン（`git clone -b yatagarasu/main`。**apex_json 対応のため要 git pull 維持**）

## 7. 動作確認の方法

- 構文: `python -m py_compile app.py get_mcap_to_csv.py` / `bash -n scripts/*.sh`
- .ps1 が ASCII のみか: `grep -P '[^\x00-\x7F]' scripts/*.ps1`（何も出なければ OK）
- 実機確認: UI で短い時間帯（1〜2 分）を VM 経由で抽出（1 回 ¥10 未満）

## 8. 新セッションの始め方（例）

> GetMcapToCsv と GetDruidUser を扱います。まず両リポジトリの docs/ にある
> HANDOVER/HANDOFF ドキュメントを読んで文脈を把握してください。
> 開発ブランチはどちらも `claude/mcap-csv-extraction-jlkg6d` です。
