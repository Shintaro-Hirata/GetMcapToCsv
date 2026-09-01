# 引き継ぎドキュメント（新セッション向け）

旧 Claude Code セッションからの引き継ぎ資料。このリポジトリの目的・設計判断・
解決済みの落とし穴・未了事項をまとめる。**新しいセッションを始めるときは、
このファイルと README.md / docs/GCP_EXECUTION.md を読ませれば文脈が復元できる。**

- 開発の本流: `main`（初期開発ブランチ `claude/mcap-csv-extraction-jlkg6d` は PR #2 で
  マージ済み。以後は作業ブランチ → main への PR 運用）
- 関連リポジトリ: `Shintaro-Hirata/GetDruidUser`（取得した CSV の取り込み先）
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
- `docs/MANUAL.md` — 初見者向けの時系列マニュアル（社内配布用の一次資料）
- `docs/GCP_EXECUTION.md` — VM 運用の完全な手順書（コスト表・トラブルシュート込み）

主な機能（実装済み。使い方は MANUAL.md）:
- **設定の保存・読み込み**: ①〜⑤の全条件（検索・ファイル/トピック/カラム選択・列名変更・
  バッチ行・出力設定）を JSON 1 ファイルで保存/復元。名前を付けて `ui_settings/` に
  ローカル保存（gitignore 済み）でき、次回は一覧から選ぶだけ。読み込みは既定で
  **車両ID・日付・時刻を復元せず①の入力を使う**（keep_period。同じトピック構成で
  別の車両・別の日を取る用途）+ **適用後に検索〜トピック一覧取得まで自動実行**
  （auto_run。抽出実行だけは課金があるため手動）
- **開始・終了日付の分離**（日跨ぎ抽出）、**分割出力**（`--split-minutes`）、
  **結合 CSV の時間軸そろえ**（`--merged-grid`/`--merged-hold`、前値ホールド）
- **列名変更**（topics JSON の `rename`。客先向け出力名）
- **バッチ実行**（⑤: 車両×期間の表を上から順に実行。VM は 1 台使い回し、2 件目以降は
  ツール転送スキップ）
- **VM ライフサイクル自動化**: 実行時に VM の実在を確認し、無ければ作成・B なら終了時に
  必ず削除（維持費ゼロ保証）・A なら停止。gcloud ログイン期限切れは実行前検知して
  再ログインを自動起動（`ensure_gcloud_auth`）
- **残 VM の後始末**（ネットワーク断対策）: ④「課金状況を確認」で VM が残っていれば
  **「残っている VM を削除する」ボタン**でその場で削除できる。VM 経由ルートを開いた
  ときにも残存をセッション 1 回だけ自動点検して警告（`ss.vm_leftover`。実行後は pop して
  再点検）。加えて VM 作成時に **自動削除タイマー**（`--max-run-duration`、既定
  `MAX_RUN_HOURS=12`、稼働時間ベース・0 で無効・非対応環境はタイマー無しへ自動
  フォールバック）を付け、削除処理が一切動けなくても課金が止まる
- **高速化一式**: 抽出並列 vCPU-1（上限 32、ディスク空きで自動制限）、1GB 以上は
  ディスクを経由せずストリーミング読み、選択トピック 0 件ファイルのスキップ、
  develop と二重記録の sensor をサマリ判定で読む前にスキップ、検索の時刻メタデータ
  ローカルキャッシュ（`mcap_cache/time_ranges.json`）、トピック一覧はセッション×種別の
  代表ファイルのみ読む（トピックごとの実体所在 = 「記録元」列も表示）
- **二重記録の解決**: 同一トピックが develop/sensor 両方にあり時間帯が重なる場合、
  行数の多い側のストリームだけ採用（`_resolve_duplicate_recordings`）

## 2. コストの考え方（結論のみ）

- GCS→PC のダウンロードは egress 課金 約 $0.12/GB（≈¥18/GB）。**トピック/カラムを
  絞ってもダウンロード量は減らない**（zstd 圧縮チャンクに全トピックが混在。実測で
  チャンクスキップ削減 0.9%、実データ比率 18.6%）。
- よって CSV 取得の本命は **VM 経由**: バケットと同一リージョンの VM 内で mcap→CSV 変換
  すれば GCS 読み込みは無料。外に出るのは CSV（数百 KB〜数十 MB ≈ ¥1 前後）だけ。
- VM は **モデル B（毎回作って消す）** を採用。待機費 ¥0。1回あたり ¥5〜10。
  `SPOT=1`（gcp.env）で稼働費約 1/3（稀に中断→再実行すればよい。VM は自動削除）。
- 既定マシンは e2-highcpu-32・ブートディスク 200GB（gcp_create_vm の既定値。gcp.env で上書き可）。抽出並列は vCPU-1・上限 32。ディスクは並列ダウンロードの一時ファイル置き場で、空きが足りない場合は並列が自動で絞られる（ENOSPC 対策）。モデル B なら存在時間だけの課金で数円/回。
- 発生しうる課金の全リストと点検方法（`gcp_status`）は GCP_EXECUTION.md にある。

## 3. データ構造の前提（重要）

- GCS パス: `YYYYMMDD_<車両>__<SSD>/recording/<セッション>/record_develop|record_sensor|record_debug_image/record_*_N.mcap`
- **ディレクトリ名の日付は運行「終了」日**。検索は開始日〜終了日+1日を見る。
- develop / sensor / image は同時刻で連番分割された兄弟ファイル。**入っているトピックが違う**
  （例: eps001 系は sensor 側。develop にはトピック名だけ登録され中身が無いことがある）。
- **同じトピックが develop と sensor の両方に「実体入りで」二重記録されている場合がある**
  （例: /t2/main_mabx/eps002。車両・日によって片側だけのこともある）。記録プロセスが
  別なので同じメッセージでも t_ns (受信時刻) が µs 単位でずれる → メッセージ単位の
  重複除去は効かず、ストリーム単位で解決する（実装済み）。
- **/t2/main_mabx/* (J1939 CAN) の単位は要注意**: EBC2 の速度・response3 の
  wheel_based_vehicle_speed は **km/h**（コード根拠: Yatagarasu の
  `odometry_data.hpp` / `control_component.cpp` に `/3.6 // Convert to m/s`）。
  EBC2 の `relative_speed_*` は**前軸速度との差分**であり絶対輪速ではない。
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
| VM 作成が `already exists` | UI 再起動で VM 運用ラジオが既定 (B) に戻り、既存 VM と衝突していた → UI は実行時に VM の実在を確認して動作を実態に合わせる（B は既存 VM でも終了時に削除 = 維持費ゼロの約束を守る）。gcp_create_vm も存在時は対処コマンドを表示 |
| 存在チェックの gcloud describe でスクリプトが即死 | 「見つからない」は正常応答なのに、PS5.1 の EAP=Stop が gcloud の stderr で例外化 → probe の前後だけ 'Continue' + `$LASTEXITCODE` 判定（上の SSH 待ちと同じ罠） |
| 高並列で `[Errno 28] No space left on device` が連鎖 | 並列数ぶんの一時ファイルがブートディスクを溢れさせた → 空き容量から並列を自動制限。読めなかったファイルがあれば末尾に目立つ警告（CSV 不完全の可能性） |
| sensor 込みで DL が 8MB/s に崩壊 | ネットワークではなく **e2 の永続ディスク書込上限 (~240MB/s)** に 31 並列が詰まっていた → 1GB 以上はディスクに落とさずストリーミング読み（ログの「DL x 秒 + 解析 y 秒」内訳で切り分け可能） |
| eps002 の CSV が 2 倍の行数 | develop/sensor の二重記録。t_ns がズレるため完全一致の重複除去では防げない → ストリーム単位で行数の多い側だけ採用（時間帯が重ならない部分は残す）。さらに読む前のサマリ判定で二重記録側のファイル自体をスキップ |
| data_editor の編集が 1 回巻き戻る（2 回入力しないと反映されない） | 編集結果を毎ランで表の入力データに書き戻していたため、widget の編集状態と入力が競合 → 入力データは世代 (batch_ver / rename_ver) ごとに固定し、編集結果は状態にだけ蓄積する |
| VM 実行のログが「installed apex_json support: True」の後、数十分無音に見える | ① SSH 経由では Python の stdout がパイプになりブロックバッファリング（8KB 溜まるまで何も表示されない）→ run_on_gcp.sh で `PYTHONUNBUFFERED=1`。② そもそも無音のフェーズがあった（--gcs-files のメタデータ取得が逐次 = 数百件で数分、二重記録の事前判定のサマリ読み、読み込み後の集計・結合 CSV 生成）→ メタデータ取得を 16 並列化し、各フェーズに開始・進捗ログ（15 秒ごと）、ファイル完了行に (x/N)、完了イベントが無い間も 60 秒ごとの生存ログを追加 |

## 5. 進行中の案件: ブリヂストン (BS) 走行データ提供【最優先の継続作業】

JIRA: [VT26-1412](https://t2auto.atlassian.net/browse/VT26-1412)（親・提供依頼 8 項目のトピック一覧あり）
/ [VT26-1413](https://t2auto.atlassian.net/browse/VT26-1413)（CSV フォーマット決定）。
依頼者: fujimaki さん（性能開発部）。上長: someya さん。

**決定済みのフォーマット**: バッチ実行 + 10 分割 + 結合 CSV「時間軸をそろえる (10ms)」+
列名変更（客先向け日本語名）。設定はユーザーの設定 JSON に保存済み。
**t_ns 列は不要と確定** → 今後の出力は④の「t_ns 列を出力しない」（CLI: `--no-t-ns`）を
オンにする（設定 JSON にも保存される）。出力済みの CSV 約 100 本は
`python drop_csv_column.py out` で一括除去できる（`--dry-run` で事前確認可）。
⚠ どちらも BS 納品用のみ。**GetDruidUser に取り込む CSV は t_ns 必須**
（src/services/csv_periods.py が t_ns 無しをエラーにする）ため既定はオフのまま。

**このセッションで確定した重要知見（列名変更の監修結果）**:
- EBC2 の速度・response3 の車速は **[m/s] ではなく [km/h]**（ツールは値変換しない）
- `relative_speed_*` は「前輪速度」ではなく**前軸速度との相対速度（差分）**。
  `rear_axle1` は第 1 後軸の意味
- `str_angle` の正字は「ハンドル**舵**角」（蛇角は誤字）。rad かどうかは実データで
  要確認（直進 ≈0、旋回で ±1 超なら rad）
- `system_state` は整数 enum → **凡例が必須**。State.idl の定義順で
  0=Terminated … 6=Ready … 16=AutonomousDriving（自動運転中）… 17=ADHandoffToDriver
- 位置 3 トピックの採否（提案済み・ユーザー採用）: `localization_compositor/pose` を
  **追加採用**（position.x/y = 地図原点基準の東/北[m]・基準点は後軸中心、heading は
  東=0 の rad。緯度経度ではない点を凡例に明記）。`main_mabx/location_data`
  （緯度経度 10Hz）は継続。`positioning_driver/inspvas` は位置としては見送り、
  **roll/pitch[deg] が欲しいか BS に確認して要るときだけ** debug.poslv_roll/pitch を採用

**座標系・正負の凡例（fujimaki さん要望・Yatagarasu コード根拠で確定）**:
- 車両座標系 (odometry local) = **x前/y左/z上の右手系**（EgoPose.idl、
  lateral_g_monitor の v×ヨーレート整合で確認）。ヨーレート＋＝左旋回、
  ロールレート＋＝右下がり、**ピッチレート＋＝前下がり**（FLU 右手系の帰結）
- localization pose = 地図座標系 ENU（PointENU.idl）。実体は **UTM ゾーン 54N**
  （localization_util.cpp: proj=utm zone=54, WGS84。「TTM = Taas Transverse Mercator」）。
  つまり x=UTM easting（中央子午線 東経141°が x=500,000m）、y=UTM northing
  （赤道から北への距離 m）。関東なら x≈40万・y≈390万 のオーダー。
  **z は WGS-84 楕円体高**（標高ではない。厳密には楕円体高×UTMスケール係数≈0.9996〜）。
  heading は東=0・反時計回り正（Pose.idl）。基準点は後軸中心。
  座標差から距離を出す場合は UTM スケール係数分（~0.04%）の縮尺誤差がある点に注意
- lateral_error ＋＝経路の左側、heading_error ＋＝経路方位より左向き
  （mpc_controller_initial_state.cpp: 基準点座標系の y / SO2 log）
- str_angle ＋＝左切り（ControlCommand.idl「Left direction: positive」、
  bicycle.hpp「left is positive」。eps/response3 系は符号反転なしで control へ）
- relative_speed_* ＝ 当該車輪速−前軸速度（odometry_data.hpp: wheel = front + relative）。
  front_axle_speed / wheel_based_vehicle_speed は J1939 のため**常に 0 以上**（後退でも正）
- poslv_roll ＋＝右下がり、**poslv_pitch ＋＝前上がり**（novatel_mapper.cpp の回転合成:
  IMU 座標は右/前/上。odometry のピッチレートと正負が逆な点に注意）。
  ※ mapper に回転順の TODO コメントがあるため、納品前に坂・カーブで符号の実測確認を推奨

**残タスク**: ①単位を修正した列名で 1 往復分（8/17-18）を再出力（STEP2）
②AD Status 凡例シート・座標系注記の作成 ③信号の図示による妥当性確認（fujimaki さん依頼。
CSV を受け取ってプロット作成を手伝う約束あり） ④STEP3: 8 月末 内容確認 & 提供

## 6. 未了事項・次のタスク候補

1. **専用 GCP プロジェクト**: 現在は t2-integration に相乗り。分離したいが、プロジェクト作成は
   組織権限が必要なため **GCP 管理者への依頼待ち**（手順は GCP_EXECUTION.md「専用プロジェクト」節）。
   作成後は gcp.env の GCP_PROJECT を書き換えるだけ。
2. **「切り出し君互換」プリセット**: `mcap_presets.json` のリストは仮。実物の対象トピックに
   合わせて更新が必要（ユーザーがリストを持ってくる）。
3. **配布準備**: ユーザーは社内配布を計画。初見者向けの docs/MANUAL.md は整備済み
   （時系列手順 + Python/gcloud のインストール手順 + トラブルシューティング）。
   zip 配布は git 不要で動く: リポジトリ直下に mcap-ros2idl-support を同梱すれば
   install_deps.bat が検出して git+https を使わずローカルからインストールする
   （zip の作り方は MANUAL 3-2 の折りたたみ）。同梱版は git pull できないため
   apex_json 更新時は zip の配り直しが必要。
   複数人利用時は gcp.env の GCP_VM を人ごとに変える（同名 VM の同時作成が衝突するため）。
4. UI 実行中の操作でストリーム処理が中断されるのは Streamlit の仕様（注意書きで対応済み。
   根本対応するなら subprocess をバックグラウンド化して進捗をポーリングする作りに変える）。

## 7. ローカル環境（git 外）に依存するもの

プラン移行ではリポジトリと下記ローカル要素があれば継続できる（PC 側に残るため作業不要）:
- `scripts/gcp.env`（gitignore 済み。GCP_PROJECT=t2-integration / ZONE=asia-northeast1-a /
  VM=mcap-csv-worker / ROS2IDL_LOCAL_PATH=zero-plotter の mcap-ros2idl-support への絶対パス）
- gcloud CLI 認証と ADC（`gcloud auth application-default login` 済み）
- zero-plotter クローン（`git clone -b yatagarasu/main`。**apex_json 対応のため要 git pull 維持**）

## 8. 動作確認の方法

- 構文: `python -m py_compile app.py get_mcap_to_csv.py` / `bash -n scripts/*.sh`
- .ps1 が ASCII のみか: `grep -P '[^\x00-\x7F]' scripts/*.ps1`（何も出なければ OK）
- 実機確認: UI で短い時間帯（1〜2 分）を VM 経由で抽出（1 回 ¥10 未満）

## 9. 動作確認の方法（追加分）

- UI の回帰は Streamlit AppTest で確認できる（セッション状態を直接セットして
  `at.run()`、`at.exception` が空であること。AppTest の session_state は
  **反復非対応**なので `for k in at.session_state` は書かない）
- 抽出コアのロジック（ストリーム解決・スキップ判定・分割・rename）は
  合成 mcap / 合成行データの単体テストで確認した実績あり（このリポジトリには
  テストを常設していないため、変更時はその場で書いて流す）

## 10. 新セッションの始め方（例）

> GetMcapToCsv と GetDruidUser を扱います。まず GetMcapToCsv の docs/HANDOVER.md を
> 読んで文脈を把握してください（BS データ提供案件の続きは HANDOVER の 5 章）。
> main が本流で、作業はブランチを切って PR で main へ。ユーザーが PR をマージするので
> マージ済みなら次の作業はブランチを main から切り直すこと。
