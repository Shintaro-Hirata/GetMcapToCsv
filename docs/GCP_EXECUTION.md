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

## Windows (PowerShell) か bash か

- **Windows で PowerShell を使う** → `.ps1` スクリプトを使う（`bash` 不要）。以下の手順は
  PowerShell 版で示す。初回だけスクリプト実行の許可が必要な場合がある:
  ```powershell
  Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
  ```
  （「実行できない」と出たときのみ。1回やれば以後不要。）
- **Git Bash / WSL / macOS / Linux** → `.sh` スクリプトを使う（`bash scripts/xxx.sh`）。
  中身は同じ。

## いちばん簡単な手順（ゼロから VM を作る）

初めてでもこの順でやれば動く。すべて手元 PC で操作する。

### 手順 0. バケットのリージョンを確認（済んでいれば飛ばす）

```bash
gcloud storage buckets describe gs://t2-ft-original-data --format="value(location)"
```
→ `ASIA-NORTHEAST1`（東京）と出る。VM も東京に作る。
（このあと出る「components update してください」の案内は無視してよい。更新は不要。）

### 手順 1a. mcap-ros2idl-support を手元に用意（/t2 デコードに必須）

`/t2/*` トピックのデコードに使う社内パッケージ。zero-plotter リポジトリの一部フォルダなので、
リポジトリを clone してそのフォルダを使う（zero-plotter の開発者でなくても read 権限があれば取得可）。

```bash
cd ~/Desktop/Dev     # 好きな場所
git clone -b yatagarasu/main https://github.com/t2-auto/zero-plotter.git
# → ~/Desktop/Dev/zero-plotter/mcap-ros2idl-support ができる
```

> **重要（apex_json トピック）**: `/t2/main_mabx/*` や `/t2/control/demand*` などは
> `apex_json` エンコードで、これを解けるのは**新しめの** mcap-ros2idl-support だけです。
> clone してから時間が経っている場合は必ず更新してください:
> ```bash
> cd ~/Desktop/Dev/zero-plotter && git pull
> ```
> 古いままだと、抽出ログに `Unknown schema encoding: apex_json` が出て、その
> トピックだけ 0 行になります（トピック一覧には名前が出るのにデータが取れない、
> という症状はこれが原因）。

### 手順 1b. 設定ファイルを用意

```powershell
Copy-Item scripts\gcp.env.example scripts\gcp.env   # PowerShell
# bash:  cp scripts/gcp.env.example scripts/gcp.env
```
`scripts/gcp.env` を開いて最低限これだけ埋める（`.ps1` も `.sh` も同じ gcp.env を読む）:
- `GCP_PROJECT` … 自分のプロジェクトID（`gcloud config get-value project` で確認）
- `GCP_ZONE` … `asia-northeast1-a`（東京。そのままでよい）
- `GCP_VM` … 好きな名前（例 `mcap-worker`）
- `ROS2IDL_LOCAL_PATH` … 手順 1a で clone した `zero-plotter/mcap-ros2idl-support` のパス

（既定は外部IP付きの標準 VM。ネットワークをより閉じたい場合のみ `PRIVATE=1` と
`GCLOUD_SSH_FLAGS="--tunnel-through-iap"` を有効化する。）

### 手順 2. VM を1台作る（1コマンド）

```powershell
.\scripts\gcp_create_vm.ps1        # PowerShell
# bash:  bash scripts/gcp_create_vm.sh
```
東京リージョンに小さな VM（4 vCPU / 16GB / ディスク100GB / バケット読み取り権限つき）を作り、
Python まで用意する。数分で終わる。

既定は**標準構成**（外部IP付き、SSH は IAM/鍵で保護）。手順が単純で詰まりにくい。
どのみちプロジェクト管理者には VM の存在は見えるため、ここは割り切って標準構成でよい。

ネットワークをより閉じたい場合は gcp.env で `PRIVATE=1`（外部IPなし + IAP SSH + OS Login）。
中に入れる・データに触れるのは自分だけになるが、IAP 用ファイアウォール作成が必要で、
組織ポリシーによっては失敗しうる（下の「セキュリティ」参照）。

### 手順 2.5. VM にバケット読み取り権限を持たせる（初回のみ）

VM の中の抽出は既定で **VM のサービスアカウント**の権限で GCS を読む。別プロジェクトの
バケット（`t2-ft-original-data`）はそのままでは読めず、`403 ... does not have
storage.objects.list access` になる。自分は読めるので、**自分の認証 (ADC) を VM に
コピー**して自分の権限で読ませる（バケット側の IAM 変更不要）。

```powershell
# 1. 手元 PC で一度だけ ADC を作る (ブラウザで認証)
gcloud auth application-default login

# 2. 抽出時に -SetupAuth を付けると ADC を VM にコピーしてから実行する (初回のみ)
.\scripts\gcp_fetch.ps1 -SetupAuth -Vehicle GIGA09 -Start "2026-07-01 20:40" -End "2026-07-01 20:45" -ExtraArgs "--list-only","--all-topics"
```
bash 版は同じ意味で `--setup-auth` を付ける:
```bash
bash scripts/gcp_fetch.sh --setup-auth --vehicle GIGA09 --start "..." --end "..." --list-only --all-topics
```
2回目以降は `-SetupAuth`（`--setup-auth`）は不要。対象ファイルが一覧表示されれば権限 OK。

> セキュリティ補足: `-SetupAuth` はあなたの個人 ADC を VM 上に置く。共有プロジェクトの
> VM で他者が SSH できる場合、その資格情報を読めてしまう点に留意。代替として、管理者に
> **VM のサービスアカウントへバケット読み取り権限**を付与してもらう手もある:
> ```
> gcloud storage buckets add-iam-policy-binding gs://t2-ft-original-data \
>   --member="serviceAccount:<VMのSA>" --role="roles/storage.objectViewer"
> ```
> （`<VMのSA>` は 403 エラーに出る `...-compute@developer.gserviceaccount.com`。
> ただし既定 SA はプロジェクト内の全 VM 共通なので、専用 SA の利用が望ましい。）

### 手順 3. 抽出する

```powershell
# PowerShell (引数は -Vehicle / -Start / -End / -Topics ...)
.\scripts\gcp_fetch.ps1 -Vehicle GIGA09 -Start "2026-07-01 20:40" -End "2026-07-01 20:45" -Topics topics.example.t2.json
```
```bash
# bash (引数は get_mcap_to_csv.py と同じ)
bash scripts/gcp_fetch.sh --vehicle GIGA09 --start "2026-07-01 20:40" \
    --end "2026-07-01 20:45" --topics topics.example.t2.json
```
重い mcap は VM の中で処理され（GCS 読み込みは無料）、手元の `out/` には CSV だけ届く。

PowerShell 版の主なオプション: `-AllTopics`（全トピック）、`-IncludeSensor` / `-IncludeImage`、
`-ExcludeTopics "/tf*","/events/*"`、その他の引数は `-ExtraArgs "--no-download"` で渡す。

### 手順 4. 使い終わったら VM を止める（課金を抑える）

手動で止める場合:
```powershell
gcloud compute instances stop <VM名> --zone asia-northeast1-a --project <プロジェクトID>
```
止めている間は VM の計算料金はかからない（ディスク分のわずかな料金のみ）。
次に使うときは `start` で再開:
```powershell
gcloud compute instances start <VM名> --zone asia-northeast1-a --project <プロジェクトID>
```

**おすすめ: `-StartStop` で自動化。** 付けると「起動 → 抽出 → 停止」を1コマンドで行い、
**エラーで落ちても必ず停止**する（止め忘れ防止）。
```powershell
.\scripts\gcp_fetch.ps1 -StartStop -Vehicle GIGA09 -Start "2026-07-01 20:40" -End "2026-07-01 20:45" -Topics topics.example.t2.json
```
bash 版は `--start-stop`:
```bash
bash scripts/gcp_fetch.sh --start-stop --vehicle GIGA09 --start "..." --end "..." --topics topics.example.t2.json
```

### 検索 UI と組み合わせる（推奨ワークフロー）

UI（`streamlit run app.py`）の「**GCS から検索**」で、①検索 → ②ファイル確認 →
③トピック・カラム選択まで手元で行い、④出力で **CSV の抽出ルート = 「VM 経由」** を
選ぶと、決めた条件（時間帯・トピック・カラム）がそのまま VM に渡って実行される。

- 検索・トピック一覧・カラム一覧で手元にダウンロードされるのはメタデータ数 MB だけ
  （egress 数円未満）。重い mcap 本体は GCP から出ない。
- 実行前に「この PC で直接抽出した場合いくらかかるか（＝節約見込み）」が表示される。
- VM 運用（B: 毎回作って消す / A: start→stop）もその場で選べる。

### かかるコストの一覧

| コスト | いつ | 目安（e2-standard-8 / 東京） | 抑え方 |
|--------|------|------------------------------|--------|
| vCPU + メモリ | 起動中のみ | 約 ¥50〜60/時（実行時間は 4vCPU の約半分） | 停止すれば ¥0。Spot なら約 1/3 |
| **ブートディスク** | **VM が存在する限り常時**（停止中も） | 30GB≒¥450/月 · 100GB≒¥1,500/月 | ディスクを小さく / VM 削除 |
| 外部 IP（ephemeral） | 起動中のみ | 約 ¥0.7/時 | 停止で解放・¥0 |
| egress（VM→PC の CSV） | 抽出時 | CSV サイズ分（数 MB≒¥0） | 実質無視 |
| GCS 操作 / 同一リージョン読み取り | 抽出時 | 1 円未満 / 無料 | 無視 |

standing（放置していてもかかる）コストは実質**ブートディスクだけ**。消し方は 2 つ:

### 運用モデル A: VM を残す + 小さいディスク + `-StartStop`（頻繁に使う人向け）

- `BOOT_DISK_GB=30` で作成（既定）。停止中のディスク代は**約 ¥450/月**。
- 毎回 `-StartStop` で起動→抽出→停止。1 回あたり実質 ¥6〜8。
- セットアップ（依存・ADC）は一度きり。すぐ使える。
```powershell
.\scripts\gcp_fetch.ps1 -StartStop -Vehicle GIGA09 -Start "..." -End "..." -Topics topics.example.t2.json
```

### 運用モデル B: 毎回 VM を作って消す（`-DeleteAfter`）（たまにしか使わない人向け・standing ¥0）

- 抽出後に **VM をディスクごと削除**するので、使っていない間の料金は**完全にゼロ**。
- 代わりに毎回 VM 作成（数分）＋依存インストール＋`-SetupAuth` が必要（時間はかかる）。
```powershell
# 1. VM を作る (毎回)
.\scripts\gcp_create_vm.ps1
# 2. ADC を入れて抽出し、終わったら VM を削除
.\scripts\gcp_fetch.ps1 -SetupAuth -DeleteAfter -Vehicle GIGA09 -Start "..." -End "..." -Topics topics.example.t2.json
```
`-DeleteAfter` はエラーで落ちても削除するので、消し忘れがない。bash 版は `--delete-after`。

> どちらでも VM は使う時だけ課金される。**月に数回なら B（standing ¥0）**、
> **週に何度も使うなら A（小さいディスクを残す）**が快適。

#### モデル B は「本当にタダ」か（正確な内訳）

- **使っていない間（VM もディスクも存在しない）→ 本当に ¥0。** 存在しないものは課金されない。
- **1 回の抽出中（VM が数分だけ存在）→ 約 ¥5〜10。** 内訳: 計算(動いた分だけ)¥3〜8 +
  CSV ダウンロード(唯一の実 egress, 76MB で ¥1.4) + ディスク/IP(数分ぶんで 1 円未満)。
- 気づかず課金され続ける典型と、その対策:
  - **VM を消してもディスクが残る** → 作成時に「VM 削除でディスクも自動削除」設定
    （gcloud 既定）。`-DeleteAfter` で消える。
  - **予約 IP が残る** → 予約 IP は使っていない（起動ごとの一時 IP・停止で解放）。
  - **クラッシュ等で VM が消えず残る**（唯一の実リスク）→ 下の点検で確認・削除できる。

#### さらに安くする: Spot VM（任意）

`gcp.env` に `SPOT=1` を書くと、`gcp_create_vm` が **Spot VM**（余剰リソースの
割安枠）で作る。実行中の計算単価が**約 1/3**（e2-standard-4 で ¥30/時 → ¥10/時前後）。

- 代償: GCP の都合で**稀に途中終了される**ことがある（短時間ジョブなら確率は低い）。
- 途中終了された場合は **VM ごと自動削除**される設定にしてあるので、課金は残らない。
  もう一度実行し直せばよい。
- 毎回作って消すモデル B と相性がよい。「たまに 1 回やり直しになってもよいから
  最安にしたい」ならオン、確実性優先ならオフのまま。
- **中断されたときの見え方**: 抽出の途中でログが
  `FATAL ERROR: Remote side unexpectedly closed network connection` のように切れて
  失敗する。これは Spot 中断の典型症状で、**そのまま再実行すれば通常は成功する**。
  頻発するなら `SPOT=1` を外す。

#### さらに速くする（参考）

モデル B の所要時間の内訳は「VM 作成 30〜60 秒 + SSH 準備 30〜60 秒 +
依存インストール 30〜60 秒 + 抽出本体 + CSV 回収」。短縮したい場合の選択肢:

1. **モデル A に切り替える**（VM を残す）— 作成・依存インストールが毎回不要になり
   数分速い。代わりに停止中ディスク代 約 ¥450/月。週に何度も使うならこちら。
2. **カスタムイメージ**（依存インストール済みの OS イメージを 1 度作って使い回す）—
   モデル B のまま依存インストールを省ける。ただしイメージ保管に月数十円かかり、
   手順も増えるため、現状の頻度なら割に合わない（必要になったら相談ください）。
3. マシンサイズ — 既定を e2-standard-8 にしてある（抽出並列 7）。ツールの並列上限が
   8 のため、これ以上大きくしても現状は速くならない。小さくしたい場合は gcp.env で
   `MACHINE_TYPE=e2-standard-4` に戻せる（実行時間は約2倍になるが時間単価は半分）。

#### 課金が残っていないかの点検（安心用・読み取りのみ）

PC がクラッシュした後などに「VM が起動したまま残っていないか」を確認できる:
```powershell
.\scripts\gcp_status.ps1     # bash: bash scripts/gcp_status.sh
```
gcp.env の VM 名の行が出なければ課金はゼロ。残っていたら表示される削除コマンドで消す。

#### 見逃しやすいコストの総点検（全リスト）

この仕組みで発生しうる課金は以下で**全部**。それ以外の GCP サービスは使っていない。

| 項目 | 発生タイミング | 目安 | 備考 |
|------|----------------|------|------|
| VM の計算資源 | VM 起動中のみ | ¥50〜60/時（e2-standard-8。Spot なら約 1/3） | 10 分の抽出で ¥8 前後 |
| ブートディスク 30GB | **VM が存在する間ずっと**（停止中も） | ¥450/月 | モデル B なら存在時間が数分なので 1 円未満 |
| 外部 IP（一時） | VM 起動中のみ | ¥0.7/時 | 予約 IP はこのツールでは作らない |
| CSV の egress（VM→PC） | CSV 回収時 | ¥18/GB（76MB で ¥1.4） | 唯一の実 egress |
| 検索 UI のメタデータ読み（PC 直） | 検索・トピック/カラム一覧・見積もり時 | 数 MB ≈ ¥1 未満/回 | mcap のフッタ・サマリのみ読む |
| GCS 操作料（list/get リクエスト） | 常時少量 | 1 円未満 | 1 万リクエストで数円のオーダー |

**このツールが作らない（＝課金され得ない）もの**: 予約静的 IP・スナップショット・
カスタムイメージ・GCS への書き込み・Cloud Logging の有料転送・ロードバランサ。
つまり、モデル B で唯一残りうるのは「クラッシュで VM が消え残った場合」だけで、
それは上の `gcp_status` で発見・削除できる。

### 既存 VM に相乗りする（VM を自分で持たない）

バケットを扱える既存 VM（例: zero-plotter の VM）に SSH できるなら、そこを使えば
**自分のディスク・VM コストはゼロ**（その VM の持ち主が費用を持つ）。gcp.env の
`GCP_VM`/`GCP_PROJECT`/`GCP_ZONE` をその VM に向け、`gcp_create_vm` は使わず
`gcp_fetch`（`-StartStop`/`-DeleteAfter` は付けない）だけ実行する。
ただし他人の VM で重い処理を回すことになるので、**持ち主の許可と SSH アクセス権**が前提。
その VM に mcap-ros2idl-support が既に入っていれば `ROS2IDL_LOCAL_PATH` も不要
（`VENV_DIR` でその環境を指す）。

---

## VM を「自分だけ」に近づける（セキュリティ・任意）

標準構成（既定）でも SSH は IAM/鍵で保護されており、他人が勝手に中に入ることはない。
さらにネットワークを閉じたい場合は gcp.env で `PRIVATE=1` にすると以下が有効になる:

| 設定 | 効果 |
|------|------|
| 外部 IP なし (`--no-address`) | インターネットから到達不可。SSH は IAP トンネルのみ |
| IAP SSH + ファイアウォール | 接続にあなたの Google 認証が必須。`35.235.240.0/20` からの tcp:22 のみ許可 |
| OS Login | SSH ログインを IAM ユーザーに限定（鍵の共有ではなくアカウント単位） |
| storage 読み取りスコープのみ | VM ができるのはバケット読み取りだけ |

**完全に隠したい場合**: 上記でも、プロジェクトの Owner/Editor には VM の存在は見える
（GCP はプロジェクト単位で権限が効くため）。それも避けたいなら、**自分専用の GCP プロジェクト**を
作ってそこに VM を置くのが唯一の完全解。その場合 `gcp.env` の `GCP_PROJECT` を専用プロジェクトに
すればスクリプトはそのまま使える（バケットは別プロジェクトのままでよいが、VM のサービスアカウントに
`t2-ft-original-data` の読み取り権限を付与する必要がある）。

## VM 作成でつまずくとき

### `プロジェクト : your-project-id` のまま / PERMISSION_DENIED

`gcp.env` の `GCP_PROJECT` を**実際のプロジェクトID**に書き換えていないと起きる。
自分が使えるプロジェクトを確認:
```bash
gcloud projects list                 # アクセスできるプロジェクト一覧
gcloud config get-value project      # 既定のプロジェクト
```
出てきた **PROJECT_ID**（表示名ではなく ID）を `gcp.env` の `GCP_PROJECT` に設定する。

### どのプロジェクトに VM を作ればよいか

`gcloud projects list` に出るのは「見えるプロジェクト」で、VM を作れるとは限らない。
**個人の作業 VM は `t2-remote-devbox`（開発者用の devbox プロジェクト）が第一候補**。
作れるかは次で事前確認できる（エラーにならなければ Compute 権限あり）:
```bash
gcloud compute instances list --project t2-remote-devbox
```
VM の**プロジェクトはバケットと別でよい**（egress 無料は同一**リージョン**であればよく、
プロジェクト一致は不要）。VM を東京（asia-northeast1）に置き、手順 2.5 でバケット読み取り
権限を持たせればよい。

### 専用プロジェクトを作って他の人のプロジェクトと分ける（任意・推奨）

他の人が管理するプロジェクト（例: t2-integration）に相乗りせず、この用途専用の
プロジェクトを作ると、課金の内訳が明確になり、誤操作のリスクも互いに無くなる。
**プロジェクト自体は何個あっても無料**（中にリソースを置いたときだけ課金）。

```bash
# 1. 組織の下にプロジェクトを作る（IDは全世界で一意。例: t2-mcap-extract-hirata）
gcloud organizations list                       # 組織IDの確認
gcloud projects create t2-mcap-extract-hirata --organization=<組織ID>

# 2. 請求先アカウントを紐付ける（これが無いと VM を作れない）
gcloud billing accounts list
gcloud billing projects link t2-mcap-extract-hirata --billing-account=<XXXXXX-XXXXXX-XXXXXX>

# 3. Compute Engine API を有効化
gcloud services enable compute.googleapis.com --project=t2-mcap-extract-hirata

# 4. gcp.env の GCP_PROJECT を新プロジェクトに書き換え → あとは今まで通り
```

**権限の注意**: 1 と 2 は組織側の権限（`resourcemanager.projects.create` と
請求先アカウントの利用権限）が必要で、**一般メンバーには無いことが多い**。
`gcloud projects create` が PERMISSION_DENIED になったら、GCP 管理者に
「mcap→CSV 変換用の小さな VM を使うだけの専用プロジェクトを 1 つ作って、
私に owner（または compute.instanceAdmin.v1）を付けてほしい。請求先は会社の
既存アカウントで」と依頼するのが早い。作ってもらえれば、あとは `gcp.env` の
`GCP_PROJECT` を書き換えるだけでスクリプト・UI はそのまま動く
（バケットへのアクセスは `-SetupAuth` であなたの認証を使うため、IAM 変更は不要）。

### `Permission denied to enable service [compute.googleapis.com]` / VM 作成が権限で失敗

そのプロジェクトで **Compute Engine を使う権限が自分に無い**。これは各自の GCP の
権限設定しだいで、ツール側では解決できない。対処は次のいずれか:

1. **Compute を使えるプロジェクトに変える** — `t2-remote-devbox` 等、権限のある所を使う。
2. **管理者に依頼する** — 対象プロジェクトで以下を有効化・付与してもらう:
   - Compute Engine API の有効化（`compute.googleapis.com`）
   - 自分のアカウントに `roles/compute.instanceAdmin.v1`（VM 作成・起動停止）
   - （最小公開 VM を使うなら）`roles/compute.securityAdmin`（ファイアウォール作成）
3. **既存の VM を間借りする（VM を自作しない）** — バケットを扱える VM
   （例: zero-plotter の VM）に SSH できるなら、`gcp_create_vm` は実行せず、`gcp.env` の
   `GCP_VM` にその VM 名、`GCP_PROJECT`/`GCP_ZONE` をその VM のものにして、
   `gcp_fetch` だけを使う。VM に mcap-ros2idl-support が既に入っていれば
   `ROS2IDL_LOCAL_PATH` は不要（`VENV_DIR` でその venv を指す手もある）。

どのプロジェクトでも Compute の権限が無い場合は、管理者に「解析用の小さな VM を1台
作れるようにしてほしい（Compute Engine の有効化と compute.instanceAdmin.v1 付与）」と
依頼するのが早い。

## 前提（うまく動かないとき確認する）

- 手元 PC に **gcloud CLI**（`gcloud auth login` 済み）。
- VM は**バケットと同一リージョン**（東京）。`gcp_create_vm.sh` を使えば自動でそうなる。
- VM のサービスアカウントに **バケット読み取り権限**（`roles/storage.objectViewer`）。
  `gcp_create_vm.sh` は読み取りスコープを付けて作るが、組織のポリシーで別途 IAM 付与が
  必要な場合がある。
- `/t2/*`（ros2idl）のデコードには **mcap-ros2idl-support** が必須。
  `ROS2IDL_LOCAL_PATH` を設定しておけば `gcp_fetch.sh` が VM へ自動で入れる
  （VM 側で GitHub 認証は不要）。zero-plotter の VM を流用する場合はその venv を
  `VENV_DIR` で指定してもよい。

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
