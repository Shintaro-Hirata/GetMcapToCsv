#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GetMcapToCsv の Streamlit UI。

起動:
    streamlit run app.py
    (Windows は start_ui.bat をダブルクリックでも可)

流れ:
    1. 車両ID・時間帯を入れて「候補ファイルを検索」(image は既定で含む / sensor は選択制)
    2. 見つかったファイルを表で確認 (1 行 1 ファイル、チェックで選択)
    3. 「トピック一覧を取得」→ 表から欲しいトピックを選択 (プリセット可)
    4. 出力形式 (CSV / mcap) と出力先を選んで「抽出実行」
"""

import contextlib
import datetime
import glob
import io
import json
import os
import shutil
import subprocess
import sys
import time

import pandas as pd
import streamlit as st

import get_mcap_to_csv as core  # find_gcs_sources, collect_topics, extract_rows, write_csvs, save_mcap_slice, download_raw_mcaps
import vm_job  # start_job, load_job, tail_job, read_log, clear_job (VM 抽出のUI非依存実行)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
PRESET_FILE = os.path.join(APP_DIR, "mcap_presets.json")
SCRIPTS_DIR = os.path.join(APP_DIR, "scripts")
SETTINGS_DIR = os.path.join(APP_DIR, "ui_settings")  # 「この PC に保存」した設定 JSON の置き場
# 検索・抽出のたびに全条件を自動保存するファイル。タブのスリープや再読み込みで
# Streamlit のセッションが失われ画面が初期状態に戻っても、ここから復元できる
AUTOSAVE_FILE = os.path.join(SETTINGS_DIR, "_autosave.json")

# 選択肢ラベルの定数。設定 JSON にはラベル文字列ではなく bool / index で保存し、
# 読み込み時にここへ引き当てるので、文言を変えても古い設定ファイルが壊れない。
INPUT_MODE_OPTIONS = ["GCS から検索", "ローカルの mcap"]
OUT_FORMAT_OPTIONS = [
    "CSV (トピック別)",
    "mcap (時間帯クロップ + トピック絞り込み → 1 ファイル)",
    "mcap (元ファイルをそのまま保存, 1 分刻み)",
]
CSV_ROUTE_OPTIONS = ["🌐 VM 経由（課金最小・推奨）", "💻 この PC で直接（mcap を全量ダウンロード）"]
VM_MODEL_OPTIONS = ["B: 毎回作って消す（待機費 ¥0）", "A: 既存 VM を start→stop"]
MERGED_MODE_OPTIONS = [
    "⏱ 時間軸をそろえる（一定周期・解析/図示向け）",
    "📜 メッセージ到着順（従来形式）",
]
MERGED_GRID_CHOICES = {"10ms": 0.01, "20ms": 0.02, "50ms": 0.05, "100ms": 0.1,
                       "200ms": 0.2, "500ms": 0.5, "1s": 1.0}
SETTINGS_TYPE = "GetMcapToCsv UI settings"  # 設定 JSON の識別子


def read_gcp_env():
    """scripts/gcp.env を読む (インラインコメント・クォート除去)。無ければ {}。"""
    path = os.path.join(SCRIPTS_DIR, "gcp.env")
    cfg = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.lstrip().startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if v[:1] in "\"'":
                    q = v[0]
                    v = v[1:].split(q, 1)[0]
                else:
                    v = v.split("#", 1)[0].strip()
                if k.isidentifier():
                    cfg[k] = v
    except FileNotFoundError:
        return {}
    return cfg


def vm_status(gcp_cfg):
    """gcp.env の VM の存在と状態を返す ("RUNNING"/"TERMINATED" 等。存在しなければ None)。

    UI の「VM 運用」選択と実際の VM の有無が食い違っても事故にならないよう、
    実行前にここで実態を確認して動作を合わせる (無ければ作成、あれば使い回し)。
    """
    vm = gcp_cfg.get("GCP_VM")
    zone = gcp_cfg.get("GCP_ZONE")
    proj = gcp_cfg.get("GCP_PROJECT")
    if not (vm and zone and proj):
        return None
    try:
        r = subprocess.run(
            f"gcloud compute instances describe {vm} --zone {zone} --project {proj} "
            f"--format=value(status)",
            shell=True, capture_output=True, text=True, timeout=60)
        return (r.stdout.strip() or None) if r.returncode == 0 else None
    except Exception:
        return None


def _vm_script_cmd(script_base, ps_args, sh_args):
    """OS に応じて .ps1 (Windows) か .sh を呼ぶコマンド列を返す。"""
    if sys.platform == "win32":
        return (["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 os.path.join(SCRIPTS_DIR, script_base + ".ps1")] + ps_args)
    return ["bash", os.path.join(SCRIPTS_DIR, script_base + ".sh")] + sh_args


def run_script_streaming(cmd, log_area):
    """スクリプト (リスト) またはシェルコマンド (文字列) を実行し、出力を逐次
    log_area に流す。戻り値: (returncode, 全出力)。文字列はシェル経由で実行する
    (Windows で gcloud.cmd を解決するため)。

    画面更新は 0.3 秒ごとに間引く。1 行ごとに更新すると、行数の多い実行で
    WebSocket 転送とブラウザ描画が膨らみ、タブが重くなる/休止されやすくなるため。
    """
    proc = subprocess.Popen(
        cmd, cwd=APP_DIR, shell=isinstance(cmd, str),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, encoding="utf-8", errors="replace")
    lines = []
    last_ui = 0.0
    for line in iter(proc.stdout.readline, ""):
        lines.append(line.rstrip("\n"))
        now = time.monotonic()
        if now - last_ui >= 0.3:
            log_area.code("\n".join(lines[-500:]))
            last_ui = now
    proc.wait()
    log_area.code("\n".join(lines[-500:]))
    return proc.returncode, "\n".join(lines)


def run_vm_job(cmd, outdir, label, log_area):
    """VM 抽出コマンドを UI から独立したプロセスとして実行し、終了まで追跡する。

    Streamlit のセッションはタブのスリープ・再読み込み・PC スリープで失われ、
    画面が初期状態に戻る。その場合でもこの方式ならジョブ本体は走り続け、
    復帰後に「未回収の VM ジョブ」から再接続してログ・結果を回収できる。
    戻り値: (returncode, 全ログ)。
    """
    vm_job.start_job(cmd, outdir, label, cwd=APP_DIR)
    return vm_job.tail_job(
        outdir, lambda text: log_area.code(text[-40000:] or "(起動中...)"))


def gcloud_token_ok(adc=False):
    """gcloud の認証トークンが今も有効か（再認証切れでないか）を静かに確認する。

    adc=False は gcloud CLI 用の認証 (gcloud auth login)、adc=True は
    Application Default Credentials (gcloud auth application-default login)。
    両者は別物で、組織のセッションポリシーによりそれぞれ独立に期限切れになる。
    """
    sub = "application-default print-access-token" if adc else "print-access-token"
    try:
        # shell=True: Windows では gcloud が gcloud.cmd のため、シェル経由で解決する
        r = subprocess.run(f"gcloud auth {sub}", shell=True,
                           capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    except Exception:
        return False


def launch_gcloud_login(adc=False):
    """gcloud のログイン (ブラウザ認証) を起動する。

    この UI のプロセス内で実行すると gcloud から「端末がない = 非対話環境」に
    見えて失敗するため、Windows では新しいコンソールウィンドウとして起動する。
    """
    cmd = "gcloud auth application-default login" if adc else "gcloud auth login"
    if sys.platform == "win32":
        subprocess.Popen(["powershell", "-NoProfile", "-Command", cmd],
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        subprocess.Popen(cmd, shell=True)


def ensure_gcloud_auth(placeholder, need_adc):
    """VM 経由ルートの実行前に gcloud 認証の生死を確認し、必要ならその場で再ログインさせる。

    gcloud のログインには有効期限があり（組織のセッションポリシー。切れるのが正常）、
    切れた状態で UI からスクリプトを実行すると gcloud が
    「Reauthentication failed. cannot prompt during non-interactive execution」で
    即失敗する。ここで先に検知し、ログイン画面を自動で開いて完了を待ってから続行する。
    戻り値: 認証が有効なら True（呼び出し側はそのまま処理を続けてよい）。
    """
    checks = [(False, "gcloud auth login", "gcloud CLI の認証")]
    if need_adc:
        checks.append((True, "gcloud auth application-default login",
                       "データ読み取り用の認証 (ADC)"))
    for adc, login_cmd, label in checks:
        placeholder.info(f"🔑 {label}を確認中...")
        if gcloud_token_ok(adc):
            continue
        launch_gcloud_login(adc)
        placeholder.warning(
            f"🔐 **{label}の期限が切れています。**再ログインの画面を起動しました\n\n"
            "- 開いたブラウザで**会社の Google アカウント**にログインしてください\n"
            "- 完了すると自動で検知して続行します（このページは操作せずお待ちください）\n"
            f"- ブラウザが開かない場合は PowerShell で `{login_cmd}` を実行してください")
        for _ in range(60):  # ログイン完了を最大 5 分待つ
            time.sleep(5)
            if gcloud_token_ok(adc):
                break
        else:
            placeholder.error(
                f"{label}のログイン完了を確認できませんでした。PowerShell で "
                f"`{login_cmd}` を実行してから、もう一度ボタンを押してください。")
            return False
    placeholder.empty()
    return True

st.set_page_config(page_title="GetMcapToCsv", page_icon="🚚", layout="wide")
st.title("🚚 GetMcapToCsv — 走行データ mcap 抽出ツール")

ss = st.session_state
ss.setdefault("sources", None)        # 検索で見つかった全ソース
ss.setdefault("search_id", 0)         # 検索のたびに増やして表をリセットする
ss.setdefault("search_log", "")
ss.setdefault("topics_info", None)    # {topic: {schema, encoding, count}}
ss.setdefault("topics_id", 0)
ss.setdefault("topics_log", "")
ss.setdefault("result_files", None)
ss.setdefault("result_log", "")
ss.setdefault("search_params", None)  # 検索時の (start_ns, end_ns, base 名) を保持
ss.setdefault("search_transfer", None)  # 検索時の GCS 転送量 (bytes)
ss.setdefault("result_transfer", None)  # 抽出時の GCS 転送量とキャッシュ利用量
ss.setdefault("topic_columns", {})      # {topic: [フラット列名, ...]} カラム絞り込み用
ss.setdefault("transfer_estimate", None)  # 転送量見積もりの結果
ss.setdefault("last_selected_topics", [])  # ③で最後に選択されたトピック (設定保存用)

# --- 入力条件の保持 -------------------------------------------------
# Streamlit はその実行で描画されなかったウィジェットの状態を破棄するため、
# 入力元の切り替え等で一時的に非表示になると値がリセットされてしまう。
# 毎回再代入して「アプリ状態」に昇格させることで、切り替え後も値を維持する。
_PERSISTED_KEYS = (
    "w_vehicle", "w_date", "w_date_end", "w_tstart", "w_tend", "w_img", "w_sen",
    "local_pattern", "local_time_filter",
    "w_bucket", "w_subdir", "w_imgsub", "w_sensub",
    "w_lookback", "w_metaw", "w_extw",
    "cache_enable", "cache_dir", "cache_max_gb",
    "w_outfmt", "w_outdir", "w_merged",
    "w_merged_mode", "w_merged_grid", "w_merged_hold", "w_split_min", "w_no_tns",
    "csv_route", "csvvm_model", "csvvm_auth",
)
for _k in list(ss.keys()):
    if _k in _PERSISTED_KEYS or str(_k).startswith("colsel_"):
        ss[_k] = ss[_k]

# 条件ウィジェットの初期値 (キー未登録のときだけ入る)
ss.setdefault("w_vehicle", "GIGA09")
ss.setdefault("w_date", datetime.date.today() - datetime.timedelta(days=1))
ss.setdefault("w_date_end", datetime.date.today() - datetime.timedelta(days=1))
ss.setdefault("w_tstart", "12:00:00")
ss.setdefault("w_tend", "12:05:00")
ss.setdefault("w_img", True)
ss.setdefault("w_sen", False)
ss.setdefault("w_bucket", core.DEFAULT_BUCKET)
ss.setdefault("w_subdir", core.DEFAULT_SUBDIR)
ss.setdefault("w_imgsub", "record_debug_image")
ss.setdefault("w_sensub", "record_sensor")
ss.setdefault("w_lookback", 24)
ss.setdefault("w_metaw", 32)
ss.setdefault("w_extw", 0)
ss.setdefault("cache_enable", True)
ss.setdefault("cache_dir", os.path.abspath(core.DEFAULT_CACHE_DIR))
ss.setdefault("cache_max_gb", float(core.DEFAULT_CACHE_MAX_GB))
ss.setdefault("w_outdir", os.path.abspath("out"))
ss.setdefault("w_merged", False)
ss.setdefault("w_merged_mode", MERGED_MODE_OPTIONS[0])
ss.setdefault("w_merged_grid", "100ms")
ss.setdefault("w_merged_hold", 5.0)
ss.setdefault("w_split_min", 0.0)
ss.setdefault("w_no_tns", False)   # t_ns 列を出力しない (GetDruidUser 取り込みには必須のため既定オフ)
ss.setdefault("col_renames", {})   # {"トピック\n列名": 出力名} CSV 列名の変更
ss.setdefault("rename_ver", 0)     # 列名変更表を作り直すためのカウンタ
ss.setdefault("batch_rows", [])    # バッチ実行の行 [{車両ID, 開始日時, 終了日時}]
ss.setdefault("batch_ver", 0)      # バッチ表を作り直すためのカウンタ


def run_captured(fn, *args, **kwargs):
    """print 出力を捕まえつつ実行し (戻り値, ログ文字列) を返す。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*args, **kwargs)
    return result, buf.getvalue()


def load_presets():
    try:
        with open(PRESET_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {k: v for k, v in raw.items() if not k.startswith("_")}
    except FileNotFoundError:
        return {}
    except Exception as e:
        st.warning(f"プリセットファイルの読み込みに失敗: {e}")
        return {}


def list_saved_settings():
    """ui_settings/ に保存済みの設定 JSON のパス一覧 (新しい順)。

    "_" 始まりは自動保存などの内部ファイルのため一覧から除く。
    """
    try:
        files = glob.glob(os.path.join(SETTINGS_DIR, "*.json"))
    except OSError:
        return []
    files = [p for p in files if not os.path.basename(p).startswith("_")]
    return sorted(files, key=os.path.getmtime, reverse=True)


def settings_summary(data):
    """設定 JSON の中身の 1 行要約 (適用前に中身を確認できるようにする)。"""
    parts = []
    if data.get("vehicle"):
        parts.append(f"車両 {data['vehicle']}")
    if data.get("date"):
        parts.append(f"期間 {data.get('date', '?')} {data.get('time_start', '')} 〜 "
                     f"{data.get('date_end', '?')} {data.get('time_end', '')}")
    parts.append(f"トピック {len(data.get('topics') or [])} 件")
    n_batch = len(data.get("batch_rows") or [])
    if n_batch:
        parts.append(f"バッチ {n_batch} 行")
    if data.get("saved_at"):
        parts.append(f"保存 {data['saved_at']}")
    return " / ".join(parts)


def parse_time_text(text):
    """キーボード入力の時刻文字列を datetime.time にする (秒まで対応)。

    受け付ける形式: "20:40" / "20:40:15" / "2040" / "204015" (全角コロンも可)
    解釈できなければ None。
    """
    s = text.strip().replace("：", ":").replace(" ", "")
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.datetime.strptime(s, fmt).time()
        except ValueError:
            pass
    if s.isdigit() and len(s) in (4, 6):
        try:
            fmt = "%H%M" if len(s) == 4 else "%H%M%S"
            return datetime.datetime.strptime(s, fmt).time()
        except ValueError:
            pass
    return None


def parse_batch_dt(text):
    """バッチ表の日時文字列を JST の datetime にする。解釈できなければ None。"""
    s = str(text or "").strip().replace("／", "/").replace("：", ":")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.datetime.strptime(s, fmt).replace(tzinfo=core.JST)
        except ValueError:
            pass
    return None


def source_kind(name):
    """パスから develop / image / sensor などの種類ラベルを取り出す。"""
    parent = name.rsplit("/", 2)
    if len(parent) >= 2:
        d = parent[-2]
        if d.startswith("record_"):
            return d[len("record_"):]
        return d
    return "?"


@st.cache_resource
def gcs_client():
    return core.gcs_client()


# ==================================================================
# 設定の保存・読み込み (①〜④の条件と選択を JSON 1 ファイルで往復させる)
# ==================================================================

def gather_settings():
    """現在の画面の条件・選択を、JSON 化できる dict として集める。

    値はセッション状態から読む (直前の操作までの内容が入っている)。
    選択肢ラベルは bool / index で持ち、文言変更に対して安定にする。
    """
    g = ss.get
    columns_selected = {}
    for t in (ss.get("topic_columns") or {}):
        sel = g(f"colsel_{t}")
        if sel:
            columns_selected[t] = list(sel)
    columns_renamed = {}
    for k, v in (ss.get("col_renames") or {}).items():
        t, _, c = k.partition("\n")
        columns_renamed.setdefault(t, {})[c] = v
    fmt = g("w_outfmt", OUT_FORMAT_OPTIONS[0])
    return {
        "_type": SETTINGS_TYPE,
        "version": 1,
        "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "input_mode_gcs": g("input_mode", INPUT_MODE_OPTIONS[0]) == INPUT_MODE_OPTIONS[0],
        "vehicle": g("w_vehicle", ""),
        "date": str(g("w_date", "")),
        "date_end": str(g("w_date_end", "")),
        "time_start": g("w_tstart", ""),
        "time_end": g("w_tend", ""),
        "include_image": bool(g("w_img", True)),
        "include_sensor": bool(g("w_sen", False)),
        "local_pattern": g("local_pattern", ""),
        "local_time_filter": bool(g("local_time_filter", True)),
        "advanced": {
            "bucket": g("w_bucket", core.DEFAULT_BUCKET),
            "subdir": g("w_subdir", core.DEFAULT_SUBDIR),
            "image_subdir": g("w_imgsub", "record_debug_image"),
            "sensor_subdir": g("w_sensub", "record_sensor"),
            "lookback": int(g("w_lookback", 24)),
            "meta_workers": int(g("w_metaw", 32)),
            "extract_workers": int(g("w_extw", 0)),
        },
        "cache": {
            "enable": bool(g("cache_enable", True)),
            "dir": g("cache_dir", os.path.abspath(core.DEFAULT_CACHE_DIR)),
            "max_gb": float(g("cache_max_gb", float(core.DEFAULT_CACHE_MAX_GB))),
        },
        "file_selection": {k: bool(v) for k, v in (ss.get("file_defaults") or {}).items()},
        "topics": list(ss.get("last_selected_topics") or []),
        "topic_columns": {t: list(v) for t, v in (ss.get("topic_columns") or {}).items()},
        "columns_selected": columns_selected,
        "columns_renamed": columns_renamed,
        "batch_rows": list(ss.get("batch_rows") or []),
        "output": {
            "format_index": OUT_FORMAT_OPTIONS.index(fmt) if fmt in OUT_FORMAT_OPTIONS else 0,
            "outdir": g("w_outdir", os.path.abspath("out")),
            "merged_csv": bool(g("w_merged", False)),
            "merged_grid_mode": (g("w_merged_mode", MERGED_MODE_OPTIONS[0])
                                 == MERGED_MODE_OPTIONS[0]),
            "merged_grid": g("w_merged_grid", "100ms"),
            "merged_hold": float(g("w_merged_hold", 5.0)),
            "split_minutes": float(g("w_split_min", 0.0)),
            "drop_t_ns": bool(g("w_no_tns", False)),
            "route_vm": g("csv_route", CSV_ROUTE_OPTIONS[0]) == CSV_ROUTE_OPTIONS[0],
            "vm_model_b": str(g("csvvm_model", VM_MODEL_OPTIONS[0])).startswith("B"),
            "vm_auth": bool(g("csvvm_auth", True)),
        },
    }


def autosave_settings():
    """現在の全条件を自動保存する (検索・抽出・バッチの実行ボタンのたびに呼ぶ)。

    タブのスリープ・再読み込み・PC スリープで Streamlit のセッションが失われて
    画面が初期状態に戻っても、ここから条件を復元できるようにする。
    """
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(AUTOSAVE_FILE, "w", encoding="utf-8") as f:
            json.dump(gather_settings(), f, ensure_ascii=False, indent=1)
    except OSError:
        pass  # 自動保存の失敗で本来の処理は止めない


def apply_settings(data, keep_period=False):
    """設定 JSON をセッション状態へ反映する。

    この関数はページ上部 (①〜④のウィジェット生成前) で呼ばれる前提。同じラン内で
    後続のウィジェットが復元値で描画される。②のファイル選択と③のトピック選択は
    検索結果に依存するため pending_* に積み、結果が揃った時点で名前を突き合わせて
    反映する。知らないキー・壊れた値は黙って読み飛ばし、適用できる分だけ適用する。

    keep_period=True のときは車両ID・取得日付・時刻をファイルから復元せず、①に
    入力済みの値をそのまま使う (同じトピック構成で別の車両・別の日を抽出する用途)。
    車両・日付に紐づく②のファイル選択も候補が変わって突き合わせられないため復元しない。
    """
    def put(key, value):
        if value is not None:
            ss[key] = value

    put("input_mode", INPUT_MODE_OPTIONS[0 if data.get("input_mode_gcs", True) else 1])
    if not keep_period:
        put("w_vehicle", data.get("vehicle"))
        try:
            if data.get("date"):
                ss["w_date"] = datetime.date.fromisoformat(str(data["date"]))
                # 終了日付を持たない古い設定ファイルは「開始と同日」として読む
                ss["w_date_end"] = ss["w_date"]
        except ValueError:
            pass
        try:
            if data.get("date_end"):
                ss["w_date_end"] = datetime.date.fromisoformat(str(data["date_end"]))
        except ValueError:
            pass
        put("w_tstart", data.get("time_start"))
        put("w_tend", data.get("time_end"))
    if "include_image" in data:
        ss["w_img"] = bool(data["include_image"])
    if "include_sensor" in data:
        ss["w_sen"] = bool(data["include_sensor"])
    put("local_pattern", data.get("local_pattern"))
    if "local_time_filter" in data:
        ss["local_time_filter"] = bool(data["local_time_filter"])

    adv = data.get("advanced") or {}
    put("w_bucket", adv.get("bucket"))
    put("w_subdir", adv.get("subdir"))
    put("w_imgsub", adv.get("image_subdir"))
    put("w_sensub", adv.get("sensor_subdir"))
    for key, name in (("w_lookback", "lookback"), ("w_metaw", "meta_workers"),
                      ("w_extw", "extract_workers")):
        try:
            if name in adv:
                ss[key] = int(adv[name])
        except (TypeError, ValueError):
            pass
    cache = data.get("cache") or {}
    if "enable" in cache:
        ss["cache_enable"] = bool(cache["enable"])
    put("cache_dir", cache.get("dir"))
    try:
        if "max_gb" in cache:
            ss["cache_max_gb"] = float(cache["max_gb"])
    except (TypeError, ValueError):
        pass

    out = data.get("output") or {}
    idx = out.get("format_index")
    if isinstance(idx, int) and 0 <= idx < len(OUT_FORMAT_OPTIONS):
        ss["w_outfmt"] = OUT_FORMAT_OPTIONS[idx]
    put("w_outdir", out.get("outdir"))
    if "merged_csv" in out:
        ss["w_merged"] = bool(out["merged_csv"])
    if "merged_grid_mode" in out:
        ss["w_merged_mode"] = MERGED_MODE_OPTIONS[0 if out["merged_grid_mode"] else 1]
    if out.get("merged_grid") in MERGED_GRID_CHOICES:
        ss["w_merged_grid"] = out["merged_grid"]
    try:
        if "merged_hold" in out:
            ss["w_merged_hold"] = float(out["merged_hold"])
    except (TypeError, ValueError):
        pass
    try:
        if "split_minutes" in out:
            ss["w_split_min"] = max(0.0, float(out["split_minutes"]))
    except (TypeError, ValueError):
        pass
    if "drop_t_ns" in out:
        ss["w_no_tns"] = bool(out["drop_t_ns"])
    if "route_vm" in out:
        ss["csv_route"] = CSV_ROUTE_OPTIONS[0 if out["route_vm"] else 1]
    if "vm_model_b" in out:
        ss["csvvm_model"] = VM_MODEL_OPTIONS[0 if out["vm_model_b"] else 1]
    if "vm_auth" in out:
        ss["csvvm_auth"] = bool(out["vm_auth"])

    # カラム候補 (topic_columns) を先に復元し、カラム選択は候補内に絞って復元する
    # (multiselect は選択値が options に無いとエラーになるため)
    tcols = {str(t): [str(c) for c in v]
             for t, v in (data.get("topic_columns") or {}).items() if isinstance(v, list)}
    if tcols:
        ss.topic_columns = {**(ss.get("topic_columns") or {}), **tcols}
        # 検索実行は topic_columns をリセットするため、読み込み直後の 1 回に限り
        # 復元したカラム候補を検索後へ引き継ぐ (検索ハンドラ側で pop される)
        ss.restored_topic_columns = dict(tcols)
    for t, sel in (data.get("columns_selected") or {}).items():
        opts = (ss.get("topic_columns") or {}).get(t)
        if opts and isinstance(sel, list):
            ss[f"colsel_{t}"] = [c for c in sel if c in opts]

    if data.get("columns_renamed"):
        for t, m in data["columns_renamed"].items():
            if isinstance(m, dict):
                for c, v in m.items():
                    if v:
                        ss["col_renames"][f"{t}\n{c}"] = str(v)
        ss.rename_ver += 1  # 列名変更表を作り直して読み込んだ出力名を反映
    if isinstance(data.get("batch_rows"), list):
        ss.batch_rows = [
            {k: str(r.get(k) or "") for k in ("車両ID", "開始日時", "終了日時")}
            for r in data["batch_rows"] if isinstance(r, dict)]
        ss.batch_ver += 1  # バッチ表を作り直して読み込んだ行を反映

    if data.get("file_selection") and not keep_period:
        ss.pending_file_selection = {str(k): bool(v)
                                     for k, v in data["file_selection"].items()}
    if isinstance(data.get("topics"), list):
        ss.loaded_topic_selection = [str(t) for t in data["topics"]]
        # プリセット/全選択が残っていると読み込んだ選択より優先されてしまうので戻す
        ss["w_topic_preset"] = "(手動選択)"
        ss["w_topic_all"] = False
        ss.topics_id += 1  # 取得済みのトピック表があれば作り直して選択を反映


with st.expander("💾 設定の保存・読み込み（①〜⑤の条件・選択を保存して使い回す）"):
    sv1, sv2 = st.columns([1, 2])
    with sv1:
        st.markdown("**保存**")
        save_name = st.text_input("設定名", key="settings_name",
                                  help="例: BS提供_結合10ms。同じ名前で保存すると上書きします。")
        if st.button("💾 この PC に保存", key="settings_save_local",
                     disabled=not save_name.strip(),
                     help="ui_settings フォルダに保存し、右の一覧からいつでも読み込めます。"):
            safe = "".join(c for c in save_name.strip() if c not in '\\/:*?"<>|')
            safe = safe or f"設定_{datetime.datetime.now():%Y%m%d_%H%M%S}"
            os.makedirs(SETTINGS_DIR, exist_ok=True)
            path = os.path.join(SETTINGS_DIR, f"{safe}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(gather_settings(), f, ensure_ascii=False, indent=1)
            st.success(f"保存しました: {os.path.relpath(path, APP_DIR)}")
        st.download_button(
            "⬇ JSON ファイルとして保存（共有用）",
            data=json.dumps(gather_settings(), ensure_ascii=False, indent=1),
            file_name=f"mcap_ui_settings_{datetime.datetime.now():%Y%m%d_%H%M%S}.json",
            mime="application/json",
            help="①の検索条件、②のファイル選択、③のトピック・カラム選択、"
                 "④の出力設定、⑤のバッチ行を JSON 1 ファイルに保存します。"
                 "他の人に条件を渡すときはこちら。")
        st.caption("保存されるのは①の検索条件・②のファイル選択・③のトピック/カラム選択・"
                   "列名変更・④の出力設定・⑤のバッチ行です。")
    with sv2:
        st.markdown("**読み込み**")
        saved_files = list_saved_settings()
        saved_names = [os.path.splitext(os.path.basename(p))[0] for p in saved_files]
        NO_PICK = "(選択してください)"
        pick_options = [NO_PICK] + saved_names
        if ss.get("settings_pick") not in pick_options:  # 削除直後などの残存値ガード
            ss["settings_pick"] = NO_PICK
        pick = st.selectbox("この PC に保存した設定", pick_options,
                            key="settings_pick",
                            help="「この PC に保存」した設定の一覧 (新しい順)。")
        up = st.file_uploader("または、設定 JSON ファイルを読み込み",
                              type=["json"], key="settings_upload")

        # 適用対象: アップロードがあればそれを、無ければ一覧の選択を使う
        load_data = None
        load_err = None
        try:
            if up is not None:
                load_data = json.loads(up.getvalue().decode("utf-8-sig"))
            elif pick != NO_PICK:
                with open(os.path.join(SETTINGS_DIR, f"{pick}.json"),
                          encoding="utf-8-sig") as f:
                    load_data = json.load(f)
        except Exception as e:
            load_err = f"設定ファイルを読み込めませんでした: {e}"
        if load_err:
            st.error(load_err)
        elif load_data is not None:
            if load_data.get("_type") != SETTINGS_TYPE:
                st.warning("このツールの設定ファイルではないようです。読める範囲で適用します。")
            st.caption("📄 " + settings_summary(load_data))

        keep_period = st.checkbox(
            "📅 車両ID・取得日付・時刻は読み込まない（①に入力した値を使う）",
            value=True, key="settings_keep_period",
            help="オン: トピック・カラム・列名変更・出力設定などだけを復元し、"
                 "車両ID・取得日付・時刻は今の①の入力のまま使います（同じトピック構成で"
                 "別の車両・別の日・別の時間帯を抽出するとき向け。車両・日付に紐づく"
                 "②のファイル選択も復元しません）。オフ: 保存時の車両ID・日付・時刻ごと"
                 "そのまま復元します。")
        auto_run = st.checkbox(
            "⚡ 適用後に「候補ファイルを検索」「トピック一覧を取得」まで自動実行",
            value=True, key="settings_auto_run",
            help="適用ボタン 1 回で③のトピック選択が反映された状態まで進みます。"
                 "あとは④の「抽出実行」を押すだけです（実行は課金を伴うため自動では"
                 "行いません）。")
        ap1, ap2 = st.columns([1, 1])
        with ap1:
            if st.button("📥 この設定を適用", key="settings_apply",
                         type="primary", disabled=load_data is None):
                apply_settings(load_data, keep_period=keep_period)
                if auto_run:
                    ss.auto_search = True  # 下の検索処理が同じランで拾って実行する
                    st.success("設定を適用し、検索とトピック一覧の取得を自動実行します...")
                else:
                    st.success("設定を適用しました。②のファイル選択と③のトピック選択は、"
                               "「候補ファイルを検索」「トピック一覧を取得」のあとに自動で反映されます。")
        with ap2:
            if pick != NO_PICK and up is None and st.button(
                    "🗑 選択した保存済み設定を削除", key="settings_delete"):
                try:
                    os.remove(os.path.join(SETTINGS_DIR, f"{pick}.json"))
                    st.rerun()
                except OSError as e:
                    st.error(f"削除できませんでした: {e}")

# ==================================================================
# セッション消失からの復帰
# タブのスリープ/再読み込み/PC スリープで Streamlit のセッション (画面の状態)
# が失われると、画面が初期状態に戻る。条件は自動保存から復元でき、VM 抽出
# ジョブは別プロセスで走り続けているので、ここから再接続して回収する。
# ==================================================================
if (ss.sources is None and not ss.get("autosave_handled")
        and os.path.exists(AUTOSAVE_FILE)):
    _autosave = None
    try:
        with open(AUTOSAVE_FILE, encoding="utf-8") as f:
            _autosave = json.load(f)
    except (OSError, ValueError):
        pass
    if _autosave:
        with st.container(border=True):
            st.markdown("♻ **前回の条件の自動保存があります** — "
                        + settings_summary(_autosave))
            st.caption("タブの再読み込みや PC スリープで画面が初期状態に戻っても、"
                       "最後に検索・抽出したときの条件をここから復元できます。")
            rc1, rc2, _ = st.columns([2, 1, 3])
            with rc1:
                if st.button("♻ この条件を復元（検索〜トピック一覧まで自動実行）",
                             key="autosave_restore", type="primary"):
                    apply_settings(_autosave, keep_period=False)
                    ss.auto_search = True
                    ss.autosave_handled = True
            with rc2:
                if st.button("閉じる", key="autosave_dismiss"):
                    ss.autosave_handled = True
                    st.rerun()

_job_outdir = ss.get("w_outdir", os.path.abspath("out"))
_job = vm_job.load_job(_job_outdir)
if _job and not ss.get("vm_job_handled"):
    with st.container(border=True):
        if not _job.get("finished"):
            st.markdown("⏳ **未回収の VM 抽出ジョブがあります**"
                        f"（開始: {_job.get('started_at', '?')}）")
            st.caption("画面が初期状態に戻っても、抽出ジョブは別プロセスで動き続けて"
                       "います（VM も動いたまま）。再接続するとログの続きを表示し、"
                       "完了まで待って結果を回収します。")
        else:
            st.markdown("📦 **前回の VM 抽出ジョブは終了しています**"
                        f"（exit {_job.get('exit_code')}、開始: {_job.get('started_at', '?')}）")
            st.caption(f"出力フォルダを確認してください: {_job_outdir}　"
                       "（ログは下のボタンで表示できます）")
        jc1, jc2, _ = st.columns([2, 1, 2])
        with jc1:
            if st.button("📡 ジョブに再接続してログ・結果を回収",
                         key="vm_job_attach", type="primary"):
                with st.expander("VM 抽出ログ（再接続）", expanded=True):
                    _area = st.empty()
                rc_job, _ = vm_job.tail_job(
                    _job_outdir,
                    lambda t: _area.code(t[-40000:] or "(ログ待ち...)"))
                ss.vm_job_handled = True
                if rc_job == 0:
                    names = []
                    try:
                        with open(os.path.join(_job_outdir, "_last_run.txt"),
                                  encoding="utf-8-sig") as mf:
                            names = [ln.strip() for ln in mf if ln.strip()]
                    except OSError:
                        pass
                    vm_job.clear_job(_job_outdir)
                    st.success(f"ジョブは正常に完了しました。CSV {len(names)} 件を "
                               f"{_job_outdir} に取得済みです。")
                    for n in names[:200]:
                        st.write(f"- `{n}`")
                else:
                    st.error("ジョブは失敗して終了しています。上のログを確認してください"
                             "（Spot 中断などは①〜④の条件のまま再実行すれば回復します）。")
        with jc2:
            if st.button("🗑 この表示を消す（記録を破棄）", key="vm_job_discard"):
                vm_job.clear_job(_job_outdir)
                ss.vm_job_handled = True
                st.rerun()

# ==================================================================
# ① 検索条件
# ==================================================================
st.header("① 検索条件")

input_mode = st.radio(
    "入力元", INPUT_MODE_OPTIONS,
    horizontal=True, key="input_mode",
    help="「GCS から検索」= ファイル・トピック・カラムを確認しながら選ぶ (推奨)。"
         "CSV は④で「VM 経由」を選べば mcap のダウンロード課金なしで取得できる。"
         "「ローカルの mcap」= 手元の mcap から抽出。")

is_gcs = input_mode.startswith("GCS から")

if is_gcs:
    col1, col2, col3, col4, col5 = st.columns([1, 1, 1, 1, 1])
    with col1:
        vehicle = st.text_input("車両ID", key="w_vehicle", help="例: GIGA07, GIGA09")
else:
    vehicle = ""
    local_pattern = st.text_input(
        "mcap のフォルダまたは glob パターン",
        value="",
        key="local_pattern",
        help=r"例: C:\data\mcap （フォルダ指定で中の *.mcap を再帰検索） / C:\data\*.mcap",
    )
    local_time_filter = st.checkbox(
        "時間帯で絞り込む", value=True, key="local_time_filter",
        help="オフにするとフォルダ内の全ファイルの全時間帯を対象にします。")
    col2, col3, col4, col5 = st.columns([1, 1, 1, 1])

with col2:
    date = st.date_input("開始日付 (JST)", key="w_date")
with col3:
    t_start_text = st.text_input("開始時刻 (HH:MM:SS)", key="w_tstart",
                                 help="秒まで指定可。例: 20:40 / 20:40:15 / 204015")
with col4:
    date_end = st.date_input("終了日付 (JST)", key="w_date_end",
                             help="日付をまたぐ実験は翌日以降を指定。")
with col5:
    t_end_text = st.text_input("終了時刻 (HH:MM:SS)", key="w_tend",
                               help="秒まで指定可。例: 20:45 / 20:45:30 / 204530")

time_needed = is_gcs or st.session_state.get("local_time_filter", True)
t_start = parse_time_text(t_start_text)
t_end = parse_time_text(t_end_text)
time_ok = t_start is not None and t_end is not None
if time_needed:
    if t_start is None:
        st.error(f"開始時刻を解釈できません: 「{t_start_text}」 (例: 20:40:15)")
    if t_end is None:
        st.error(f"終了時刻を解釈できません: 「{t_end_text}」 (例: 20:45:30)")
if not time_ok:
    t_start = t_start or datetime.time(0, 0)
    t_end = t_end or datetime.time(0, 0)

if is_gcs:
    icol1, icol2, _ = st.columns([1, 1, 2])
    with icol1:
        include_image = st.checkbox(
            "record_debug_image も含める", key="w_img",
            help="develop と同じ連番の image ファイルも対象に加える。"
                 "CSV 抽出だけが目的なら、オフにすると GCS 転送量 (課金) を大きく減らせます。")
    with icol2:
        include_sensor = st.checkbox("record_sensor も含める", key="w_sen",
                                     help="develop と同じ連番の sensor ファイルも対象に加える (サイズ大)。"
                                          "sensor には develop と別のトピックが入っているので、"
                                          "sensor 由来のトピックを③で選びたい場合はオンにする。"
                                          "CSV を VM 経由で取る場合は④側でも sensor を読めます。")

with st.expander("詳細オプション"):
    oc1, oc2, oc3 = st.columns(3)
    with oc1:
        bucket = st.text_input("GCS バケット", key="w_bucket")
        subdir = st.text_input("基準サブディレクトリ", key="w_subdir")
    with oc2:
        image_subdir = st.text_input("image サブディレクトリ名", key="w_imgsub")
        sensor_subdir = st.text_input("sensor サブディレクトリ名", key="w_sensub")
    with oc3:
        lookback = st.number_input("セッション遡り時間 (h)", min_value=1, max_value=96,
                                   key="w_lookback")
        meta_workers = st.number_input("メタデータ並列数", min_value=1, max_value=64,
                                       key="w_metaw")
        extract_workers = st.number_input("抽出並列数 (0=自動)", min_value=0, max_value=32,
                                          key="w_extw")

    st.markdown("**ローカルキャッシュ**（同じ mcap の再ダウンロード = 再課金を防ぐ）")
    cc1, cc2, cc3 = st.columns([1, 2, 1])
    with cc1:
        cache_enable = st.checkbox("キャッシュを使う", key="cache_enable")
    with cc2:
        cache_dir_input = st.text_input("キャッシュフォルダ", key="cache_dir")
    with cc3:
        cache_max_gb = st.number_input("上限 (GB)",
                                       min_value=1.0, max_value=500.0, step=5.0,
                                       key="cache_max_gb")
    csize = core.cache_total_size(cache_dir_input)
    ccol1, ccol2 = st.columns([2, 1])
    with ccol1:
        st.caption(f"現在のキャッシュ: {core.size_str(csize)}")
    with ccol2:
        if csize and st.button("🗑 キャッシュをクリア"):
            shutil.rmtree(cache_dir_input, ignore_errors=True)
            st.rerun()

cache_dir = cache_dir_input if cache_enable else None

start_dt = datetime.datetime.combine(date, t_start, tzinfo=core.JST)
end_dt = datetime.datetime.combine(date_end, t_end, tzinfo=core.JST)
range_ok = True
if end_dt <= start_dt:
    if date_end == date:
        end_dt += datetime.timedelta(days=1)  # 同日で終了が開始より前なら深夜跨ぎとみなす
        if time_ok and time_needed:
            st.info(f"終了時刻が開始時刻以前のため翌日扱いにします: 終了 = {end_dt:%m/%d %H:%M:%S}")
    else:
        range_ok = False
        if time_ok and time_needed:
            st.error("終了日時が開始日時以前になっています。開始/終了の日付と時刻を確認してください。")
if time_ok and time_needed and range_ok:
    dur_h = (end_dt - start_dt).total_seconds() / 3600
    st.caption(f"🕐 抽出時間帯: {start_dt:%Y-%m-%d %H:%M:%S} 〜 {end_dt:%Y-%m-%d %H:%M:%S} "
               f"(JST, 約 {dur_h:.1f} 時間)")
    if dur_h > 24:
        st.caption("⚠ 24 時間を超える範囲です。対象ファイル数・処理時間・費用が大きくなる"
                   "ことがあるため、②の合計サイズを確認してから進んでください。")

_btn_label = "🔍 ① 候補ファイルを検索" if is_gcs else "📂 ① ローカル mcap を読み込み"
# 設定の適用 (自動実行オン) から渡されるフラグ。同じランで検索まで進める
auto_search = bool(ss.pop("auto_search", False))
if auto_search and time_needed and (not time_ok or not range_ok):
    st.warning("設定を適用しましたが、①の日付・時刻が不正なため自動検索は行いません。"
               "①を直してから「候補ファイルを検索」を押してください。")
    auto_search = False
if st.button(_btn_label, type="primary",
             disabled=(time_needed and (not time_ok or not range_ok))) or auto_search:
    # 起動ターミナル側の生存確認ログ。「ボタンを押しても何も起きない」ときに、
    # クリックがサーバーへ届いているか (WebSocket 断か処理側の問題か) を切り分ける。
    print(f"[ui] 検索開始: vehicle={vehicle!r} "
          f"{start_dt:%Y-%m-%d %H:%M:%S} - {end_dt:%Y-%m-%d %H:%M:%S} (JST)", flush=True)
    autosave_settings()  # セッションが失われても条件を復元できるようにする
    ss.sources = None
    ss.topics_info = None
    ss.result_files = None
    ss.file_defaults = None  # 新しい検索結果ではチェック状態を全選択に戻す
    ss.search_transfer = None
    ss.topic_columns = ss.pop("restored_topic_columns", None) or {}
    ss.transfer_estimate = None
    core.STATS.reset()
    if is_gcs:
        try:
            with st.spinner("GCS を検索中... (セッション特定 → 時刻メタデータで絞り込み)"):
                sources, log = run_captured(
                    core.find_gcs_sources, gcs_client(), bucket, vehicle,
                    start_dt, end_dt, subdir=subdir, lookback_hours=int(lookback),
                    workers=int(meta_workers),
                    include_sensor=include_sensor, sensor_subdir=sensor_subdir,
                    include_image=include_image, image_subdir=image_subdir)
            ss.sources = sources
            ss.search_id += 1
            ss.search_log = log
            ss.search_transfer = core.STATS.snapshot()
            # 日付をまたぐ範囲は、出力ファイル名の終了側にも日付を入れて曖昧さを無くす
            end_fmt = "%Y%m%d_%H%M%S" if end_dt.date() != start_dt.date() else "%H%M%S"
            ss.search_params = {
                "start_ns": core.to_ns(start_dt),
                "end_ns": core.to_ns(end_dt),
                "base": f"{vehicle.strip().upper()}_{start_dt:%Y%m%d_%H%M%S}-"
                        f"{end_dt.strftime(end_fmt)}",
                "base_prefix": vehicle.strip().upper(),  # 分割出力の区間別ファイル名用
                "extract_workers": int(extract_workers) or None,
            }
            if auto_search:
                ss.auto_topics = True  # 続けて③のトピック一覧取得まで自動で行う
        except LookupError as e:
            st.error(f"見つかりませんでした: {e}")
            ss.search_log = ""
        except Exception as e:
            st.error(f"検索に失敗しました: {e}")
    else:
        pattern = (local_pattern or "").strip().strip('"')
        if not pattern:
            st.error("mcap のフォルダまたはパターンを入力してください。")
        else:
            if os.path.isdir(pattern):
                paths = sorted(glob.glob(os.path.join(pattern, "**", "*.mcap"),
                                         recursive=True))
            else:
                paths = sorted(glob.glob(pattern))
            if not paths:
                st.error(f"mcap が見つかりません: {pattern}")
            else:
                sources = [core.LocalMcapSource(p) for p in paths]
                use_filter = bool(st.session_state.get("local_time_filter", True))
                log = ""
                if use_filter:
                    with st.spinner("時刻メタデータで絞り込み中..."):
                        sources, log = run_captured(
                            core.filter_sources_by_time, sources,
                            core.to_ns(start_dt), core.to_ns(end_dt), 8)
                if not sources:
                    st.error("指定時間帯に重なる mcap がありません。")
                    ss.search_log = log
                else:
                    ss.sources = sources
                    ss.search_id += 1
                    ss.search_log = log
                    folder = os.path.basename(os.path.dirname(os.path.abspath(paths[0]))) or "local"
                    end_fmt = "%Y%m%d_%H%M%S" if end_dt.date() != start_dt.date() else "%H%M%S"
                    base = (f"{folder}_{start_dt:%Y%m%d_%H%M%S}-{end_dt.strftime(end_fmt)}"
                            if use_filter else folder)
                    ss.search_params = {
                        "start_ns": core.to_ns(start_dt) if use_filter else None,
                        "end_ns": core.to_ns(end_dt) if use_filter else None,
                        "base": base,
                        "base_prefix": folder,  # 分割出力の区間別ファイル名用
                        "extract_workers": int(extract_workers) or None,
                    }
                    if auto_search:
                        ss.auto_topics = True  # 続けて③のトピック一覧取得まで自動で行う

if ss.get("search_transfer"):
    g_b, c_b = ss.search_transfer
    if g_b or c_b:
        st.caption(f"📡 この検索での GCS 読み込み: {core.size_str(g_b)} "
                   f"(egress {core.cost_str(g_b)})")
if ss.search_log:
    with st.expander("検索ログ"):
        st.code(ss.search_log)

# ==================================================================
# ② 候補ファイルの確認・選択 (1 行 1 ファイル)
# ==================================================================
selected_sources = []
if ss.sources:
    st.header("② 対象ファイル")

    src_by_name = {s.name: s for s in ss.sources}
    names = [s.name for s in ss.sources]

    # チェック状態の初期値 (一括操作ボタンで書き換え、表を作り直して反映する)
    ss.setdefault("file_ver", 0)
    if ss.get("file_defaults") is None or set(ss.file_defaults) != set(names):
        ss.file_defaults = {n: True for n in names}
        ss.file_ver += 1

    # 設定ファイルから読み込んだファイル選択があれば、今回の候補と名前で突き合わせて反映
    pend_files = ss.pop("pending_file_selection", None)
    if pend_files:
        hit = 0
        for n in names:
            if n in pend_files:
                ss.file_defaults[n] = bool(pend_files[n])
                hit += 1
        ss.file_ver += 1
        if hit < len(pend_files):
            st.caption(f"⚠ 読み込んだ設定のファイル選択のうち {len(pend_files) - hit} 件は"
                       "今回の候補に存在しないため無視しました。")

    current = dict(ss.file_defaults)  # {パス: 抽出対象か}

    file_rows = []
    for i, s in enumerate(ss.sources):
        rng = getattr(s, "time_range", None)
        file_rows.append({
            "No.": i + 1,
            "抽出": "✅" if current.get(s.name, True) else "－",
            "種類": source_kind(s.name),
            "ファイル名": s.name.rsplit("/", 1)[-1],
            "時間帯 (JST)": (f"{core.fmt_jst(rng[0])} - {core.fmt_jst(rng[1])}"
                          if rng else "(時刻不明)"),
            "サイズ": core.size_str(s.size),
            "セッション": s.name.split("/recording/")[-1].split("/")[0]
                        if "/recording/" in s.name else "",
            "パス": s.name,
        })
    st.caption("行をクリックで選択 (Shift+クリックで範囲選択、左端のチェックで複数選択)。"
               "選んだ行に対して下のボタンで「抽出」列を切り替えます。")
    event = st.dataframe(
        pd.DataFrame(file_rows),
        column_config={
            "No.": st.column_config.NumberColumn("No.", width="small"),
            "抽出": st.column_config.TextColumn("抽出", width="small"),
            "パス": None,  # フルパスは非表示 (選択サマリで確認可能)
        },
        hide_index=True,
        use_container_width=True,
        on_select="rerun",
        selection_mode="multi-row",
        key=f"file_table_{ss.search_id}_{ss.file_ver}",
    )
    picked_rows = list(event.selection.rows) if event and event.selection else []
    picked_names = [names[i] for i in picked_rows if 0 <= i < len(names)]

    def apply_file_selection(new_map):
        ss.file_defaults = new_map
        ss.file_ver += 1  # 表を作り直して「抽出」列と行選択をリセット
        st.rerun()

    # --- 選択行への操作 ---
    has_pick = bool(picked_names)
    bc1, bc2, bc3, bc4 = st.columns([1, 1, 1, 3])
    with bc1:
        if st.button(f"✅ 選択行を含める ({len(picked_names)})", disabled=not has_pick):
            m = dict(current)
            for n in picked_names:
                m[n] = True
            apply_file_selection(m)
    with bc2:
        if st.button(f"🚫 選択行を外す ({len(picked_names)})", disabled=not has_pick):
            m = dict(current)
            for n in picked_names:
                m[n] = False
            apply_file_selection(m)
    with bc3:
        if st.button(f"🔁 選択行を入替 ({len(picked_names)})", disabled=not has_pick):
            m = dict(current)
            for n in picked_names:
                m[n] = not m.get(n, True)
            apply_file_selection(m)

    # --- 全体への操作 ---
    gc1, gc2, gc3 = st.columns([1, 1, 4])
    with gc1:
        if st.button("☑ 全て含める"):
            apply_file_selection({n: True for n in names})
    with gc2:
        if st.button("☐ 全て外す"):
            apply_file_selection({n: False for n in names})
    with gc3:
        kinds = sorted({source_kind(n) for n in names})
        kcols = st.columns(max(len(kinds), 1))
        for kc, kind in zip(kcols, kinds):
            with kc:
                if st.button(f"{kind} のみ"):
                    apply_file_selection({n: source_kind(n) == kind for n in names})

    selected_sources = [src_by_name[n] for n in names if current.get(n, True)]
    sel_size = sum(s.size or 0 for s in selected_sources)
    st.caption(f"✅ 選択中: {len(selected_sources)} / {len(ss.sources)} ファイル "
               f"(合計 {core.size_str(sel_size)})")
    with st.expander("選択中のファイルを確認 (フルパス)"):
        for s in selected_sources:
            st.text(s.name)

    # ==============================================================
    # ③ トピック選択 (1 行 1 トピック)
    # ==============================================================
    st.header("③ トピック選択")
    tcol1, tcol2, tcol3 = st.columns([1, 2, 1])
    with tcol1:
        # 設定の適用 (自動実行オン) → 検索成功のあと、同じランでここまで自動で進む
        auto_topics = bool(ss.pop("auto_topics", False))
        if st.button("📋 トピック一覧を取得") or auto_topics:
            with st.spinner("トピック一覧を取得中..."):
                info, log = run_captured(core.collect_topics, selected_sources)
            ss.topics_info = info
            ss.topics_id += 1
            ss.topics_log = log
            if not info:
                st.warning("トピックが取得できませんでした。")

    presets = load_presets()
    selected_topics = []

    if ss.topics_info:
        all_topic_names = list(ss.topics_info.keys())
        preset_options = ["(手動選択)"] + list(presets.keys())
        if ss.get("w_topic_preset") not in preset_options:
            ss["w_topic_preset"] = "(手動選択)"
        with tcol2:
            preset_name = st.selectbox(
                "プリセット (選ぶと下の表の選択に反映)",
                preset_options, key="w_topic_preset")
        with tcol3:
            st.write("")
            use_all = st.checkbox("全トピック選択", value=False, key="w_topic_all")

        # 表の「選択」の初期値。手動選択のときは保持している選択 (設定ファイルの
        # 読み込み結果や前回までの手動選択) を毎回初期値にする。表の初期値は
        # 再描画のたびに作り直されるため、一度きりの適用では次の再描画で消えてしまう。
        loaded_topics = ss.get("loaded_topic_selection")
        if use_all:
            default_set = set(all_topic_names)
        elif preset_name == "(手動選択)" and loaded_topics is not None:
            loaded_set = set(loaded_topics)
            default_set = {t for t in all_topic_names if t in loaded_set}
            missing_saved = sorted(loaded_set - default_set)
            if missing_saved:
                st.caption("⚠ 選択済みでデータに存在しないトピック: "
                           + ", ".join(missing_saved[:10])
                           + (" ..." if len(missing_saved) > 10 else ""))
        elif preset_name != "(手動選択)":
            wanted = presets[preset_name]
            default_set = {t for t in all_topic_names
                           if any(core.fnmatch(t, p) for p in wanted)}
            missing = [p for p in wanted
                       if not any(core.fnmatch(t, p) for t in all_topic_names)]
            if missing:
                st.caption(f"⚠ プリセット中でデータに存在しないもの: {', '.join(missing)}")
        else:
            default_set = set()

        topic_rows = [{
            "選択": t in default_set,
            "トピック": t,
            "記録元": "+".join(ss.topics_info[t].get("kinds") or []) or "-",
            "メッセージ数": ss.topics_info[t]["count"],
            "エンコード": ss.topics_info[t]["encoding"],
            "スキーマ": ss.topics_info[t]["schema"],
        } for t in all_topic_names]
        edited_topics = st.data_editor(
            pd.DataFrame(topic_rows),
            column_config={
                "選択": st.column_config.CheckboxColumn("選択", width="small"),
                "トピック": st.column_config.TextColumn("トピック", width="large"),
                "記録元": st.column_config.TextColumn(
                    "記録元", width="small",
                    help="このトピックの実体 (メッセージ) がある記録種別。develop のみ"
                         "なら①の「record_sensor も含める」は不要 (速くなる)。"
                         "「-」は代表ファイル内にメッセージが無かったことを示す。"),
            },
            disabled=["トピック", "記録元", "メッセージ数", "エンコード", "スキーマ"],
            hide_index=True,
            use_container_width=True,
            height=420,
            key=f"topic_editor_{ss.topics_id}_{preset_name}_{use_all}",
        )
        selected_topics = list(edited_topics[edited_topics["選択"]]["トピック"])
        ss.last_selected_topics = selected_topics  # 設定保存 (gather_settings) で参照する
        if not use_all and preset_name == "(手動選択)":
            # 手動選択の内容を保持し、次回の表の初期値にする (再取得しても選択が残る)
            ss.loaded_topic_selection = list(selected_topics)
        st.caption(f"✅ 選択中: {len(selected_topics)} / {len(all_topic_names)} トピック")
        if selected_topics:
            with st.expander("選択中のトピックを確認"):
                for t in selected_topics:
                    st.text(t)

        # --- カラム絞り込み (CSV 出力の列を選ぶ。既定は全カラム) ---
        if selected_topics:
            with st.expander("🧩 カラム絞り込み・列名変更（任意・CSV のみ）"):
                st.caption(
                    "トピックごとに CSV へ出力する列を選べます (既定は全カラム)。"
                    "※ mcap はメッセージ単位で取得するため、列を絞っても GCS の"
                    "ダウンロード量は変わりません。CSV のサイズと見やすさの改善用です。")
                if st.button("🔎 選択トピックのカラム一覧を取得"):
                    with st.spinner("カラム名を取得中... (各トピック数メッセージだけデコード)"):
                        cols_map, log = run_captured(
                            core.sample_topic_columns, selected_sources,
                            selected_topics, cache_dir)
                    ss.topic_columns.update(cols_map)
                    missed = [t for t in selected_topics if t not in ss.topic_columns]
                    if missed:
                        st.warning("カラムを取得できなかったトピック: " + ", ".join(missed))
                for t in selected_topics:
                    opts = ss.topic_columns.get(t)
                    if not opts:
                        continue
                    st.multiselect(
                        t, options=opts, default=opts,  # 取得直後は全選択
                        key=f"colsel_{t}",
                        help="× で外した列は CSV に出力されません。全部外すと全カラム扱い。")
                # --- 列名の変更 (任意): 出力 CSV のヘッダを分かりやすい名前にする ---
                ren_rows = []
                for t in selected_topics:
                    opts = ss.topic_columns.get(t)
                    if not opts:
                        continue
                    sel = st.session_state.get(f"colsel_{t}")
                    use = sel if sel and 0 < len(sel) < len(opts) else opts
                    for c in use:
                        ren_rows.append({"トピック": t, "元の列名": c,
                                         "出力名": ss.col_renames.get(f"{t}\n{c}", "")})
                if ren_rows:
                    st.markdown("**✏ 列名の変更（任意）** — 客先向けの分かりやすい名前などを"
                                "「出力名」列に入力（空欄なら元の列名のまま）。"
                                "トピック別 CSV と結合 CSV(時間軸そろえ) のヘッダに反映され、"
                                "設定 JSON にも保存されます。")
                    ren_key = (abs(hash(tuple(f"{r['トピック']}|{r['元の列名']}"
                                              for r in ren_rows))) % 100000,
                               ss.rename_ver)
                    # 表の入力データは行構成 (ren_key) ごとに固定する。編集結果を
                    # 毎ランで入力へ書き戻すと data_editor の編集状態と競合し、
                    # 直前の編集が一度巻き戻って見える (2 回入力しないと反映されない)
                    seed_key = f"rename_seed_{ren_key}"
                    if seed_key not in ss:
                        for k in [k for k in ss.keys()
                                  if str(k).startswith("rename_seed_") and k != seed_key]:
                            del ss[k]
                        ss[seed_key] = pd.DataFrame(ren_rows)
                    edited_ren = st.data_editor(
                        ss[seed_key],
                        disabled=["トピック", "元の列名"],
                        hide_index=True, use_container_width=True,
                        height=min(400, 42 + 35 * len(ren_rows)),
                        key=f"rename_editor_{ren_key}")
                    for _, r in edited_ren.iterrows():
                        rk = f"{r['トピック']}\n{r['元の列名']}"
                        v = str(r["出力名"] or "").strip()
                        if v:
                            ss.col_renames[rk] = v
                        else:
                            ss.col_renames.pop(rk, None)
                    vals = list(ss.col_renames.values())
                    dups = sorted({v for v in vals if vals.count(v) > 1})
                    if dups:
                        st.warning("出力名が重複しています（列の区別がつかなくなります）: "
                                   + ", ".join(dups))
                if not any(t in ss.topic_columns for t in selected_topics):
                    st.caption("「カラム一覧を取得」を押すと、ここで列の絞り込みと"
                               "列名の変更ができます。")
    else:
        st.caption("「トピック一覧を取得」を押すと、ここで選択できます。"
                   " (mcap を元ファイルのまま保存する場合はトピック選択は不要)")

    # ==============================================================
    # ④ 出力
    # ==============================================================
    st.header("④ 出力")
    ocol1, ocol2 = st.columns([1, 1])
    with ocol1:
        out_format = st.radio("出力形式", OUT_FORMAT_OPTIONS, key="w_outfmt")
    with ocol2:
        outdir = st.text_input("出力フォルダ", key="w_outdir")
        merged_csv = st.checkbox("結合 CSV も出力 (全トピックを 1 ファイルに)", key="w_merged",
                                 help="選択した全トピックを 1 本の CSV に結合 (CSV のみ)。"
                                      "形式は下の「結合 CSV の形式」で選ぶ。")

    # 結合 CSV の形式: 周波数が違うトピックの混在に備え、共通時間軸へ揃える形式を既定にする
    merged_grid_sec = None
    merged_hold_sec = None
    if merged_csv and out_format.startswith("CSV"):
        mm1, mm2, mm3 = st.columns([2, 1, 1])
        with mm1:
            merged_mode = st.radio(
                "結合 CSV の形式", MERGED_MODE_OPTIONS, horizontal=True, key="w_merged_mode",
                help="「時間軸をそろえる」= 一定周期の時刻グリッドを作り、各セルにその時刻"
                     "までの最新値を入れる (前値ホールド)。周波数が異なるトピックを混ぜても"
                     "歯抜けにならず、そのまま Excel 等でグラフ化できる。列名は"
                     "「トピック.列名」。「メッセージ到着順」= 1 メッセージ 1 行の従来形式"
                     " (他トピックの列は空欄になる)。")
        if merged_mode == MERGED_MODE_OPTIONS[0]:
            with mm2:
                grid_label = st.selectbox("周期", list(MERGED_GRID_CHOICES.keys()),
                                          key="w_merged_grid",
                                          help="出力する時刻グリッドの間隔。データより細かく"
                                               "しても情報は増えない (前値が並ぶだけ)。")
            with mm3:
                hold = st.number_input("前値保持の上限 (秒)", min_value=0.0, step=1.0,
                                       key="w_merged_hold",
                                       help="この秒数を超えて更新が無い区間は空欄にする"
                                            " (実際のデータ欠損が見えるように)。0 で無制限。")
            merged_grid_sec = MERGED_GRID_CHOICES[grid_label]
            merged_hold_sec = float(hold)

    # 分割出力: 長時間の抽出でも 1 ファイルが巨大にならないよう、書き出しを分数で刻む
    split_min = 0.0
    drop_t_ns = False
    if out_format.startswith("CSV"):
        spc1, spc2 = st.columns([1, 3])
        with spc1:
            split_min = float(st.number_input(
                "⏳ 分割出力 (分, 0 = 分割なし)", min_value=0.0, step=10.0,
                key="w_split_min",
                help="指定した分数ごとにファイルを区切って出力する (例: 30 →"
                     " 12:00-12:30, 12:30-13:00, ...)。mcap の読み込みは 1 回のままで、"
                     "書き出しだけを分割するので追加の課金・時間はほぼ無い。"
                     "各ファイルの t_sec はその区間の先頭からの経過秒になる。"))
        with spc2:
            st.write("")
            drop_t_ns = st.checkbox(
                "🧹 t_ns 列を出力しない（客先納品用）", key="w_no_tns",
                help="時刻列のうち t_ns (epoch ナノ秒) を CSV から除きます"
                     " (time_jst と t_sec は残ります)。"
                     "⚠ GetDruidUser に取り込む CSV では t_ns が必須のため、"
                     "その用途ではオフのままにしてください。")

    # --- CSV の抽出ルート (GCS のみ): この PC で直接 or GCP 内の VM 経由 ---
    # ここで決めた時間帯・トピック・カラムをそのまま VM に渡せるので、
    # 「選びやすさは検索 UI、ダウンロード課金は VM 経由」の両取りができる。
    csv_route_vm = False
    vm_route_ready = True
    if is_gcs and out_format.startswith("CSV"):
        route = st.radio(
            "CSV の抽出ルート", CSV_ROUTE_OPTIONS,
            horizontal=True, key="csv_route",
            help="VM 経由: GCP 内に VM を用意し、そこで mcap→CSV 変換して CSV だけ回収"
                 "（重い mcap は GCP から出ないため egress 課金がほぼ 0。準備に数分）。"
                 "直接: この PC に mcap を丸ごとダウンロードして抽出（すぐ始まるが課金あり）。")
        csv_route_vm = route.startswith("🌐")
        if csv_route_vm:
            gcp_cfg = read_gcp_env()
            vm_route_ready = (
                bool(gcp_cfg.get("GCP_PROJECT"))
                and gcp_cfg.get("GCP_PROJECT") != "your-project-id"
                and bool(gcp_cfg.get("GCP_VM"))
                and gcp_cfg.get("GCP_VM") != "your-vm-name")
            if not vm_route_ready:
                st.error("scripts/gcp.env が未設定のため VM 経由は使えません。"
                         "GCP_VM に自分用の VM 名（例: mcap-worker-yamada）を設定するなど、"
                         "docs/MANUAL.md 3-5 の手順で設定してください。")
            if selected_sources:
                # 直接ルートで発生するダウンロード量 = 選択ファイル合計。
                # mcap はチャンク単位でしか読めず、トピック/カラムを絞っても
                # ダウンロード量はほぼ変わらない (この量は選択ファイルで決まる)。
                dl_direct = sel_size
                est = ss.get("transfer_estimate")
                if est and est[1].get("needed"):
                    dl_direct = min(dl_direct, est[1]["needed"])
                st.info(
                    f"💰 **節約見込み ≈ {core.cost_str(dl_direct)}**\n\n"
                    f"- この PC で直接: mcap 約 {core.size_str(dl_direct)} をダウンロード "
                    f"(egress {core.cost_str(dl_direct)})\n"
                    f"- VM 経由: mcap は GCP 内で処理。手元に来るのは CSV だけ"
                    f"（通常数 MB〜数十 MB ≈ ¥1 前後）\n"
                    f"- 別途 VM 稼働費: 約 ¥150〜180/時（32vCPU・実行中のみ。実行が速い分 "
                    f"1 回 ¥10〜30 程度、Spot 設定ならさらに約 1/3）")
                st.caption(
                    "※ この金額は「選んだ**ファイル**の合計サイズ」で決まり、"
                    "トピックを 1 個にしても全部にしても変わりません。mcap は圧縮チャンク"
                    "単位でしか読めず、トピック/カラムの絞り込みは GCS ダウンロード量を"
                    "減らさないためです（絞り込みで小さくなるのは出力 CSV の方）。"
                    "ファイル自体を②で減らす・sensor を外すと、この金額が下がります。")
            vmc1, vmc2 = st.columns([2, 1])
            with vmc1:
                st.radio("VM 運用", VM_MODEL_OPTIONS,
                         horizontal=True, key="csvvm_model",
                         help="B = **終了後に VM が残らないことを保証**（維持費 ¥0。"
                              "既存 VM があってもそれを使い、終了時に削除する）。"
                              "A = VM を残して使い回す（停止中もディスク代 月 ~¥450 かかるが"
                              "立ち上がりが速い）。どちらも VM が無ければ自動で作成するので、"
                              "選択と実態のずれで失敗はしない。")
            with vmc2:
                st.checkbox("認証を VM に入れる（新規 VM は必須）", value=True, key="csvvm_auth",
                            help="手元の gcloud auth application-default login の認証を VM へコピー。")
            st.caption("※ VM は **②で選択したファイルだけ**を読みます（develop/sensor の"
                       "別も②の選択どおり。image は CSV に使わないため自動で除外）。"
                       "結合 CSV の有無・形式も上の設定に従います。")
            # --- VM の課金点検と後始末 --------------------------------
            # ネットワーク断などで抽出が中断すると自動削除が動かず、VM が
            # 残って課金が続くことがある。残存をここで検知し、1 クリックで
            # 削除できるようにする (VM 経由を開いた最初の 1 回は自動点検)。
            if vm_route_ready and "vm_leftover" not in ss:
                with st.spinner("VM が残っていないか確認中..."):
                    ss.vm_leftover = vm_status(gcp_cfg)
            if st.button("🔍 課金状況を確認（VM が残っていないか）", key="csvvm_status"):
                if ensure_gcloud_auth(st.empty(), need_adc=False):
                    with st.spinner("確認中..."):
                        _, status_out = run_script_streaming(
                            _vm_script_cmd("gcp_status", [], []), st.empty())
                    ss.vm_status_log = status_out
                    ss.vm_leftover = vm_status(gcp_cfg)
            if ss.get("vm_status_log"):
                with st.expander("課金状況の確認結果", expanded=True):
                    st.code(ss.vm_status_log)
            leftover = ss.get("vm_leftover")
            if leftover:
                vm_name = gcp_cfg.get("GCP_VM", "")
                if leftover == "RUNNING":
                    st.warning(
                        f"⚠ VM 「{vm_name}」が**起動したまま残っています**（状態: {leftover}）。"
                        "いま抽出を実行中でなければ計算課金が続いています（前回の実行が"
                        "ネットワーク断などで中断した場合に起こります）。下のボタンで削除できます。")
                else:
                    vm_model_b_now = str(ss.get("csvvm_model",
                                                VM_MODEL_OPTIONS[0])).startswith("B")
                    note = ("B 運用では実行のたびに自動で作り直されるので、削除して問題ありません。"
                            if vm_model_b_now else
                            "A 運用（使い回し）で意図して残している場合は削除不要です。")
                    st.info(f"VM 「{vm_name}」が残っています（状態: {leftover}。停止中でも"
                            f"ディスク代 月 ~¥3,000/200GB がかかります）。{note}")
                if st.button("🗑 残っている VM を削除する（課金を止める）", key="vm_delete"):
                    if ensure_gcloud_auth(st.empty(), need_adc=False):
                        with st.expander("VM 削除ログ", expanded=True):
                            rc, _ = run_script_streaming(
                                f"gcloud compute instances delete {vm_name} "
                                f"--zone {gcp_cfg.get('GCP_ZONE')} "
                                f"--project {gcp_cfg.get('GCP_PROJECT')} --quiet",
                                st.empty())
                        if rc == 0:
                            ss.vm_leftover = None
                            ss.pop("vm_status_log", None)  # 削除前の結果は古いため破棄
                            st.success("VM を削除しました（この用途での課金は止まりました）。"
                                       "次回の実行時には自動で作り直されます。")
                        else:
                            st.error("VM の削除に失敗しました。上のログを確認してください。"
                                     "gcloud の認証切れの場合は、もう一度ボタンを押してください。")
            elif ss.get("vm_status_log"):
                st.success("VM は残っていません（この用途での課金はありません）。")

    can_run = bool(selected_sources) and (
        out_format.startswith("mcap (元ファイル") or bool(selected_topics))
    if not can_run and selected_sources:
        st.caption("CSV / mcap(絞り込み) はトピックを 1 つ以上選択してください。")
    if csv_route_vm and not vm_route_ready:
        can_run = False

    # --- 転送量 (課金) の事前見積もり ---
    if selected_sources and selected_topics and is_gcs:
        if st.button("📉 転送量を見積もる (選択トピックでどれだけ減るか)"):
            with st.spinner("サマリを読んで見積もり中... (数MB程度の読み込み)"):
                (rows, agg), est_log = run_captured(
                    core.estimate_transfer_report, selected_sources, selected_topics,
                    ss.search_params["start_ns"], ss.search_params["end_ns"],
                    cache_dir)
            ss.transfer_estimate = (rows, agg)
        if ss.get("transfer_estimate"):
            rows, agg = ss.transfer_estimate
            if rows:
                est_df = pd.DataFrame([{
                    "ファイル": r["name"].rsplit("/", 1)[-1],
                    "全体": core.size_str(r["total_bytes"]),
                    "必要チャンク": core.size_str(r["needed_bytes"]),
                    "削減率": (f"{100 * (1 - r['needed_bytes'] / r['total_bytes']):.1f}%"
                              if r["total_bytes"] else "-"),
                    "キャッシュ": "済" if r["cached"] else "",
                } for r in rows])
                st.dataframe(est_df, hide_index=True, use_container_width=True)
                total, needed = agg["total"], agg["needed"]
                pct = 100.0 * (1 - needed / total) if total else 0.0
                lines = [f"チャンクスキップ後の転送量: {core.size_str(needed)} / "
                         f"全体 {core.size_str(total)} (削減 {pct:.1f}%, "
                         f"{core.cost_str(total)} → {core.cost_str(needed)})"]
                if agg["sel_uncomp"] is not None and agg["win_uncomp"]:
                    approx = "≈" if agg.get("ratio_sampled") else ""
                    share = 100.0 * agg["sel_uncomp"] / agg["win_uncomp"]
                    lines.append(f"選択トピックの実データ比率: {approx}{share:.1f}% (非圧縮換算)")
                    if agg["compressions"] <= {"none", ""}:
                        lines.append(f"チャンクが非圧縮のため、メッセージ単位取得を実装すれば"
                                     f"理論上 約{100 - share:.0f}% 削減の余地あり (未実装・要相談)")
                    else:
                        comp = "/".join(sorted(agg["compressions"]))
                        lines.append(f"チャンクは圧縮済み ({comp}) のため、これ以上の削減は"
                                     " GCP 内での実行 (egress 無料) が必要")
                st.info("📉 " + "\n\n".join(lines))
                st.caption("削減が 40% 以上見込めるファイルは、抽出時に自動で部分読み込みに"
                           "切り替わります (それ未満は丸ごとダウンロード + キャッシュ)。")

    st.caption("⏳ 実行中にページ内の他のボタンや入力を操作すると処理が中断されます"
               "（Streamlit の仕様）。完了表示が出るまでそのままお待ちください。")
    def build_topic_config():
        """③の選択トピックとカラム絞り込み・列名変更から CLI 用のトピック設定を作る。
        一部の列だけ選ばれていれば絞り込む (全選択/空選択は全カラム扱い)。"""
        config = {}
        for t in selected_topics:
            cfg_t = {"suffix": t.strip("/").replace("/", "_"), "fields": []}
            opts = ss.topic_columns.get(t)
            sel = st.session_state.get(f"colsel_{t}")
            if opts and sel and 0 < len(sel) < len(opts):
                cfg_t["columns"] = list(sel)
            ren = {c: ss.col_renames[f"{t}\n{c}"]
                   for c in (opts or []) if f"{t}\n{c}" in ss.col_renames}
            if ren:
                cfg_t["rename"] = ren
            config[t] = cfg_t
        return config

    def add_output_flags(ps, sh):
        """④の結合 CSV・分割の設定を gcp_fetch の引数 (ps1/sh) に反映する。"""
        if not merged_csv:
            ps.append("-NoMerged"); sh.append("--no-merged")
        elif merged_grid_sec:
            ps += ["-MergedGrid", f"{merged_grid_sec:g}",
                   "-MergedHold", f"{merged_hold_sec:g}"]
            sh += ["--merged-grid", f"{merged_grid_sec:g}",
                   "--merged-hold", f"{merged_hold_sec:g}"]
        if split_min:
            ps += ["-SplitMinutes", f"{split_min:g}"]
            sh += ["--split-minutes", f"{split_min:g}"]
        if drop_t_ns:
            ps.append("-NoTNs"); sh.append("--no-t-ns")

    if st.button("🚀 ④ 抽出実行", type="primary", disabled=not can_run):
        print(f"[ui] 抽出開始: {out_format} / ファイル {len(selected_sources)} 件 / "
              f"トピック {len(selected_topics)} 件", flush=True)
        autosave_settings()  # セッションが失われても条件を復元できるようにする
        params = ss.search_params
        os.makedirs(outdir, exist_ok=True)

        # --- VM 経由 (CSV のみ): 検索 UI で決めた条件を topics JSON にして VM へ渡す ---
        if out_format.startswith("CSV") and csv_route_vm:
            # gcloud 認証が期限切れのまま進むと VM 作成が必ず失敗するので、先に確認する。
            # ADC は「認証を VM に入れる」をオンにしたときだけ使うので、そのときだけ見る。
            if not ensure_gcloud_auth(
                    st.empty(), need_adc=st.session_state.get("csvvm_auth", True)):
                st.stop()
            topic_config = build_topic_config()
            topics_path = os.path.join(outdir, "_vm_topics.json")
            with open(topics_path, "w", encoding="utf-8") as f:
                json.dump(topic_config, f, ensure_ascii=False, indent=1)

            # ②で選択したファイルをそのまま VM に渡す (VM 側の再検索をスキップ)。
            # image は CSV に使わないので除外して転送量・時間を節約する。
            image_kind = image_subdir[len("record_"):] if image_subdir.startswith("record_") \
                else image_subdir
            vm_files = [s.name for s in selected_sources
                        if source_kind(s.name) != image_kind]
            n_img = len(selected_sources) - len(vm_files)
            if n_img:
                st.caption(f"（image ファイル {n_img} 件は CSV に使わないため除外しました）")
            files_path = os.path.join(outdir, "_vm_files.json")
            with open(files_path, "w", encoding="utf-8") as f:
                json.dump(vm_files, f, ensure_ascii=False, indent=1)

            s_str = f"{datetime.datetime.fromtimestamp(params['start_ns'] / 1e9, core.JST):%Y-%m-%d %H:%M:%S}"
            e_str = f"{datetime.datetime.fromtimestamp(params['end_ns'] / 1e9, core.JST):%Y-%m-%d %H:%M:%S}"
            ps = ["-Vehicle", vehicle, "-Start", s_str, "-End", e_str,
                  "-Topics", topics_path, "-GcsFiles", files_path, "-LocalOut", outdir]
            sh = ["--vehicle", vehicle, "--start", s_str, "--end", e_str,
                  "--topics", topics_path, "--gcs-files", files_path, "--local-out", outdir]
            add_output_flags(ps, sh)
            if st.session_state.get("csvvm_auth", True):
                ps.append("-SetupAuth"); sh.append("--setup-auth")
            vm_model_b = str(st.session_state.get("csvvm_model", "B")).startswith("B")
            # 「VM 運用」の選択と実際の VM の有無が食い違っていても失敗させない:
            # 無ければモデルに関わらず作成し、既にあれば作成せず使う。
            # B の約束は「終了後に VM が存在しない = 維持費ゼロ」なので、
            # B では既存 VM でも終了後に必ず削除する (開始時に明示する)。
            status = vm_status(read_gcp_env())
            create_first = status is None
            if vm_model_b and not create_first:
                st.info("既存の VM が見つかりました。作成せずにこの VM で実行し、"
                        "**B 運用のため終了後に削除します**（維持費を残さないため）。"
                        "VM を残したい場合は「A: 既存 VM を start→stop」を選んでください。")
            elif not vm_model_b and create_first:
                st.info("VM が存在しないため、先に作成します（A 運用のため実行後も残ります）。")
            if vm_model_b:
                ps.append("-DeleteAfter"); sh.append("--delete-after")
            else:
                ps.append("-StartStop"); sh.append("--start-stop")

            t_run0 = time.time()
            ok = True
            # 各フェーズのログは独立した expander に流す。こうすると②が①を
            # 上書きせず、両方のログが最後まで残る。見出しをクリックすると
            # そのフェーズのログを開閉できる（配布時にも全ログを追える）。
            if create_first:
                st.info("① VM を作成します（1〜2 分）...")
                with st.expander("① VM 作成ログ（クリックで開閉）", expanded=True):
                    create_log = st.empty()
                rc, _ = run_script_streaming(
                    _vm_script_cmd("gcp_create_vm", ["-Yes"], ["--yes"]), create_log)
                ok = rc == 0
                if not ok:
                    st.error("VM 作成に失敗しました。上の「① VM 作成ログ」を確認してください。")
            if ok:
                st.info("② VM で抽出し、CSV を回収します...（この間に画面が初期状態に"
                        "戻ってしまっても、ジョブは動き続けます。ページ上部の"
                        "「未回収の VM 抽出ジョブ」から再接続してください）")
                with st.expander("② 抽出・CSV 回収ログ（クリックで開閉）", expanded=True):
                    fetch_log = st.empty()
                rc, fetch_out = run_vm_job(
                    _vm_script_cmd("gcp_fetch", ps, sh), outdir,
                    f"{vehicle} {s_str} 〜 {e_str}", fetch_log)
                if rc == 0:
                    vm_job.clear_job(outdir)  # 回収済み。次回起動時の再接続表示を出さない
                    # gcp_fetch が書き出す「このランで生成した分」だけを表示する
                    # (出力フォルダに残る過去ランの CSV と混同しないため)
                    manifest = os.path.join(outdir, "_last_run.txt")
                    names = []
                    try:
                        with open(manifest, encoding="utf-8-sig") as mf:
                            names = [ln.strip() for ln in mf if ln.strip()]
                    except OSError:
                        names = [os.path.basename(p)
                                 for p in sorted(glob.glob(os.path.join(outdir, "*.csv")))
                                 if os.path.getmtime(p) >= t_run0 - 5]
                    st.success(f"完了: このランで CSV {len(names)} 件を {outdir} に取得しました"
                               "（mcap は GCP 内で処理したため egress 課金はほぼ 0）。"
                               "※ フォルダには過去ランの CSV も残っています。")
                    for n in names[:200]:
                        p = os.path.join(outdir, n)
                        sz = f" ({core.size_str(os.path.getsize(p))})" if os.path.exists(p) else ""
                        st.write(f"- `{n}`{sz}")
                else:
                    # 抽出の途中で SSH 接続が切れた = VM が実行中に消えた/落ちた兆候。
                    # Spot VM の中断 (プリエンプション) が代表的で、その場合はただ再実行すればよい。
                    dropped = any(s in (fetch_out or "") for s in (
                        "unexpectedly closed", "Connection reset",
                        "Connection to", "connection closed", "Broken pipe"))
                    spot_on = str(read_gcp_env().get("SPOT", "")).strip() == "1"
                    st.error("抽出に失敗しました。ログを確認してください。"
                             "（VM は自動で削除/停止済みのはずですが、"
                             "「課金状況を確認」でも確認できます）")
                    if dropped:
                        msg = ("実行の**途中で VM への接続が切れました**"
                               "（ログ末尾に『unexpectedly closed』等）。処理中に VM が"
                               "消えた/落ちたときの症状です。\n\n")
                        if spot_on:
                            msg += ("**最有力は Spot VM の中断です**（`SPOT=1`）。Spot は"
                                    "GCP の都合でまれに強制終了されます。害はなく、"
                                    "**もう一度「抽出実行」を押すだけ**で通常は成功します。"
                                    "頻発するなら scripts/gcp.env の `SPOT=1` を外して"
                                    "（確実な通常 VM に）ください。")
                        else:
                            msg += ("一時的なネットワーク断か、VM のリソース不足の可能性が"
                                    "あります。まず**再実行**してください。続くようなら対象"
                                    "ファイル数を減らすか、マシンを大きく（gcp.env の "
                                    "`MACHINE_TYPE`）してください。")
                        st.warning(msg)
                    else:
                        st.warning(
                            "0 行 (メッセージが見つからない) で失敗した場合、主な原因は次の 2 つです:\n\n"
                            "1. **apex_json 非対応**: ログに『Unknown schema encoding: apex_json』"
                            "がある場合、手元の zero-plotter が古く apex_json トピック "
                            "(/t2/main_mabx/*, /t2/control/demand* など) を解けません。"
                            "`cd zero-plotter && git pull` で最新 (yatagarasu/main) に更新して再実行してください。\n\n"
                            "2. **トピックの入っているファイルを②で外している**: sensor 由来の"
                            "トピックなら sensor ファイルを、develop 由来なら develop ファイルを"
                            "②で選択して再実行してください。")
            ss.pop("vm_leftover", None)  # VM の有無が変わったため、次の描画で再点検する
            st.stop()

        prog = st.progress(0.0, text="準備中...")
        core.STATS.reset()
        try:
            if out_format.startswith("CSV"):
                topic_config = build_topic_config()

                def on_file_done(done, total, name):
                    prog.progress(done / total,
                                  text=f"CSV 抽出中... {done}/{total} ファイル完了 "
                                       f"(直近: {name.rsplit('/', 1)[-1]})")

                prog.progress(0.0, text=f"CSV 抽出中... 0/{len(selected_sources)} ファイル完了 "
                                        "(ダウンロード + デコードには数分かかることがあります)")

                def run():
                    per_topic = core.extract_rows(
                        selected_sources, topic_config,
                        params["start_ns"], params["end_ns"],
                        workers=params["extract_workers"],
                        progress=on_file_done,
                        cache_dir=cache_dir)
                    hold = merged_hold_sec if merged_hold_sec is not None else 5.0
                    if split_min:
                        return core.write_csvs_split(
                            per_topic, topic_config, outdir,
                            params.get("base_prefix", params["base"]),
                            params["start_ns"], params["end_ns"], split_min,
                            merged=merged_csv, merged_grid=merged_grid_sec,
                            merged_hold=hold, drop_t_ns=drop_t_ns)
                    return core.write_csvs(per_topic, topic_config, outdir,
                                           params["base"], merged=merged_csv,
                                           merged_grid=merged_grid_sec,
                                           merged_hold=hold, drop_t_ns=drop_t_ns)
                files, log = run_captured(run)
            elif out_format.startswith("mcap (時間帯"):
                out_path = os.path.join(outdir, f"{params['base']}_cropped.mcap")

                def on_slice(done, total, name):
                    prog.progress(done / total,
                                  text=f"mcap 切り出し中... {done}/{total} ファイル処理済み "
                                       f"(直近: {name.rsplit('/', 1)[-1]})")

                (out_path, n), log = run_captured(
                    core.save_mcap_slice, selected_sources, selected_topics,
                    params["start_ns"], params["end_ns"], out_path,
                    progress=on_slice)
                files = [out_path] if n else []
                if n == 0:
                    st.warning("該当メッセージが 0 件でした。トピック選択を確認してください。")
            else:
                total_size = sum(s.size or 0 for s in selected_sources)
                t0 = time.monotonic()

                def on_dl(done_b, total_b, done_f, total_f):
                    frac = (done_b / total_b) if total_b else (done_f / max(total_f, 1))
                    elapsed = time.monotonic() - t0
                    extra = ""
                    if done_b > 0 and elapsed > 1.0:
                        speed = done_b / elapsed
                        remain = (total_b - done_b) / speed if speed > 0 else 0
                        m, s_ = divmod(int(remain), 60)
                        extra = (f" — {core.size_str(speed)}/s, 残り約 "
                                 + (f"{m}分{s_:02d}秒" if m else f"{s_}秒"))
                    prog.progress(min(1.0, frac),
                                  text=f"ダウンロード中... {core.size_str(done_b)} / "
                                       f"{core.size_str(total_b)} ({frac * 100:.1f}%) "
                                       f"[{done_f}/{total_f} ファイル完了]{extra}")

                prog.progress(0.0, text=f"ダウンロード開始... 合計 {core.size_str(total_size)}")
                files, log = run_captured(
                    core.download_raw_mcaps, selected_sources, outdir,
                    4, on_dl, cache_dir)
            prog.progress(1.0, text="✅ 完了")
            if cache_dir:
                core.prune_cache(cache_dir, float(cache_max_gb))
            ss.result_files = files
            ss.result_log = log
            ss.result_transfer = core.STATS.snapshot()
            if files:
                st.balloons()
        except Exception as e:
            prog.progress(0.0, text="❌ 失敗")
            st.error(f"抽出に失敗しました: {e}")

    if ss.result_files is not None:
        if ss.get("result_transfer"):
            g_b, c_b = ss.result_transfer
            parts = []
            if g_b:
                parts.append(f"GCS 読み込み: {core.size_str(g_b)} (egress {core.cost_str(g_b)})")
            if c_b:
                parts.append(f"キャッシュ利用: {core.size_str(c_b)} (節約 {core.cost_str(c_b)})")
            if not g_b and not c_b:
                parts.append("GCS 読み込みなし (ローカルのみ)")
            st.info("💰 " + " / ".join(parts))
        if ss.result_files:
            st.success(f"完了: {len(ss.result_files)} ファイルを出力しました")
            for fpath in ss.result_files:
                try:
                    st.write(f"- `{fpath}`  ({core.size_str(os.path.getsize(fpath))})")
                except OSError:
                    st.write(f"- `{fpath}`")
            if st.button("📂 出力フォルダを開く"):
                if sys.platform == "win32":
                    os.startfile(outdir)  # noqa: 出力先をエクスプローラで開く
                else:
                    subprocess.Popen(["xdg-open", outdir])
        else:
            st.warning("出力ファイルがありません。ログを確認してください。")
        with st.expander("実行ログ", expanded=not ss.result_files):
            st.code(ss.result_log or "(ログなし)")

    # ==============================================================
    # ⑤ バッチ実行 (任意): 複数の車両・期間を同じ設定でまとめて抽出
    # ==============================================================
    if is_gcs and out_format.startswith("CSV") and selected_topics:
        st.header("⑤ バッチ実行（任意）")
        with st.expander("🌙 複数の車両・期間をまとめて抽出（夜間実行向け）"):
            st.caption(
                "③のトピック・カラム・列名変更と④の出力設定（結合 CSV・分割・抽出ルート）を"
                "全行に適用し、上から順に抽出します。対象ファイルは各行の条件から**自動で**"
                "絞り込みます（②の手動選択は使いません。record_sensor を含めるかは"
                "①のチェックに従い、image は含めません）。"
                "実行中はこの画面を操作しないでください。**PC がスリープしない設定**に"
                "しておくこと（夜間実行時）。")
            # ①の検索条件をそのまま 1 行分にしたもの (初期行と「行を追加」で使う)
            _seed_row = {"車両ID": (vehicle or "").strip().upper(),
                         "開始日時": f"{start_dt:%Y-%m-%d %H:%M:%S}",
                         "終了日時": f"{end_dt:%Y-%m-%d %H:%M:%S}"}
            # 表の入力データは世代 (batch_ver) ごとに固定する。編集結果 (batch_rows) を
            # 毎ランで入力へ書き戻すと data_editor の編集状態と競合し、直前の編集が
            # 一度巻き戻って見える (2 回入力しないと反映されない)
            seed_key = f"batch_seed_{ss.batch_ver}"
            if seed_key not in ss:
                for k in [k for k in ss.keys()
                          if str(k).startswith("batch_seed_") and k != seed_key]:
                    del ss[k]
                ss[seed_key] = pd.DataFrame(ss.batch_rows or [_seed_row],
                                            columns=["車両ID", "開始日時", "終了日時"])
            edited_batch = st.data_editor(
                ss[seed_key],
                num_rows="dynamic", hide_index=True, use_container_width=True,
                key=f"batch_editor_{ss.batch_ver}",
                column_config={
                    "車両ID": st.column_config.TextColumn("車両ID", help="例: GIGA11"),
                    "開始日時": st.column_config.TextColumn(
                        "開始日時", help="例: 2026-08-18 12:00"),
                    "終了日時": st.column_config.TextColumn(
                        "終了日時", help="例: 2026-08-18 18:00"),
                })
            rows_b = [{k: str(r.get(k) or "").strip()
                       for k in ("車両ID", "開始日時", "終了日時")}
                      for _, r in edited_batch.iterrows()]
            ss.batch_rows = [r for r in rows_b if any(r.values())]
            if st.button("➕ ①の検索条件を行として追加", key="batch_add",
                         help="①で入力中の車両ID・開始/終了日時を表の末尾に追加します。"
                              "①を書き換えてこのボタンを押す、を繰り返すと楽に行を作れます。"):
                ss.batch_rows = ss.batch_rows + [dict(_seed_row)]
                ss.batch_ver += 1  # 表を作り直して追加行を反映
                st.rerun()
            batch_jobs = []
            n_bad = 0
            for r in ss.batch_rows:
                sdt = parse_batch_dt(r["開始日時"])
                edt = parse_batch_dt(r["終了日時"])
                if r["車両ID"] and sdt and edt and sdt < edt:
                    batch_jobs.append((r["車両ID"].upper(), sdt, edt))
                else:
                    n_bad += 1
            if n_bad:
                st.warning(f"{n_bad} 行は車両ID または日時（例: 2026-08-18 12:00）を"
                           "解釈できないため実行対象外です。")
            route_note = "VM 経由" if csv_route_vm else "この PC で直接（ダウンロード課金あり）"
            if st.button(f"🌙 バッチ実行 ({len(batch_jobs)} 件, {route_note})",
                         disabled=not batch_jobs, key="batch_run"):
                print(f"[ui] バッチ開始: {len(batch_jobs)} 件", flush=True)
                autosave_settings()  # セッションが失われても条件を復元できるようにする
                if csv_route_vm and not ensure_gcloud_auth(
                        st.empty(), need_adc=st.session_state.get("csvvm_auth", True)):
                    st.stop()
                os.makedirs(outdir, exist_ok=True)
                topic_config = build_topic_config()
                results = []
                if csv_route_vm:
                    topics_path = os.path.join(outdir, "_vm_topics.json")
                    with open(topics_path, "w", encoding="utf-8") as f:
                        json.dump(topic_config, f, ensure_ascii=False, indent=1)
                    vm_model_b = str(st.session_state.get("csvvm_model", "B")).startswith("B")
                    gcp_cfg = read_gcp_env()
                    vm_args = (f"{gcp_cfg.get('GCP_VM', '')} "
                               f"--zone {gcp_cfg.get('GCP_ZONE', '')} "
                               f"--project {gcp_cfg.get('GCP_PROJECT', '')}")
                    # バッチ中は VM を 1 台使い回す。無ければ作成し、既にあれば
                    # そのまま使う。B の約束は「終了後に VM が存在しない = 維持費
                    # ゼロ」なので、B では既存 VM でも終了時に必ず削除する
                    end_note = ("バッチ終了時に VM を削除します（B 運用・維持費ゼロ）"
                                if vm_model_b else "バッチ終了時に VM を停止します（A 運用）")
                    status = vm_status(gcp_cfg)
                    if status is None:
                        st.info(f"VM を作成します（バッチ全体で使い回し、{end_note}）...")
                        with st.expander("VM 作成ログ", expanded=False):
                            rc, _ = run_script_streaming(
                                _vm_script_cmd("gcp_create_vm", ["-Yes"], ["--yes"]),
                                st.empty())
                        if rc != 0:
                            st.error("VM 作成に失敗したため中止します。")
                            st.stop()
                    elif status != "RUNNING":
                        st.info(f"既存の VM を起動します（バッチ全体で使い回し、{end_note}）...")
                        with st.expander("VM 起動ログ", expanded=False):
                            run_script_streaming(
                                f"gcloud compute instances start {vm_args}", st.empty())
                    else:
                        st.info(f"既存の VM が起動中のため、そのまま使い回します（{end_note}）。")
                    try:
                        consec_fail = 0
                        for i, (veh, sdt, edt) in enumerate(batch_jobs):
                            label = f"{veh} {sdt:%Y-%m-%d %H:%M} 〜 {edt:%Y-%m-%d %H:%M}"
                            st.info(f"({i + 1}/{len(batch_jobs)}) {label} を抽出中...")
                            s_str = f"{sdt:%Y-%m-%d %H:%M:%S}"
                            e_str = f"{edt:%Y-%m-%d %H:%M:%S}"
                            ps = ["-Vehicle", veh, "-Start", s_str, "-End", e_str,
                                  "-Topics", topics_path, "-LocalOut", outdir]
                            sh = ["--vehicle", veh, "--start", s_str, "--end", e_str,
                                  "--topics", topics_path, "--local-out", outdir]
                            if include_sensor:
                                ps.append("-IncludeSensor"); sh.append("--include-sensor")
                            add_output_flags(ps, sh)
                            if i == 0 and st.session_state.get("csvvm_auth", True):
                                ps.append("-SetupAuth"); sh.append("--setup-auth")
                            if i > 0:
                                # ツールと mcap-ros2idl-support は 1 件目で転送済み。
                                # 2 件目以降は転送を省略して 30 秒〜1 分/件を節約する
                                ps.append("-SkipPush"); sh.append("--skip-push")
                            with st.expander(f"ログ: {label}", expanded=False):
                                rc, _out = run_script_streaming(
                                    _vm_script_cmd("gcp_fetch", ps, sh), st.empty())
                            results.append((label, rc == 0))
                            consec_fail = 0 if rc == 0 else consec_fail + 1
                            if consec_fail >= 2:
                                st.error("2 件連続で失敗したため残りを中断します。"
                                         "ログを確認してから再実行してください。")
                                break
                    finally:
                        if vm_model_b:
                            st.info("VM を削除します（維持費を残さないため）...")
                            with st.expander("VM 削除ログ", expanded=False):
                                run_script_streaming(
                                    f"gcloud compute instances delete {vm_args} --quiet",
                                    st.empty())
                        else:
                            st.info("VM を停止します（A 運用のため削除はしません）...")
                            with st.expander("VM 停止ログ", expanded=False):
                                run_script_streaming(
                                    f"gcloud compute instances stop {vm_args}", st.empty())
                        ss.pop("vm_leftover", None)  # VM の有無が変わったため、次の描画で再点検する
                else:
                    core.STATS.reset()
                    hold = merged_hold_sec if merged_hold_sec is not None else 5.0
                    for i, (veh, sdt, edt) in enumerate(batch_jobs):
                        label = f"{veh} {sdt:%Y-%m-%d %H:%M} 〜 {edt:%Y-%m-%d %H:%M}"
                        prog_b = st.progress(0.0, text=f"({i + 1}/{len(batch_jobs)}) "
                                                       f"{label} を検索中...")
                        try:
                            srcs, log1 = run_captured(
                                core.find_gcs_sources, gcs_client(), bucket, veh,
                                sdt, edt, subdir=subdir, lookback_hours=int(lookback),
                                workers=int(meta_workers),
                                include_sensor=include_sensor,
                                sensor_subdir=sensor_subdir,
                                include_image=False, image_subdir=image_subdir)
                            s_ns, e_ns = core.to_ns(sdt), core.to_ns(edt)
                            prog_b.progress(0.3, text=f"({i + 1}/{len(batch_jobs)}) "
                                                      f"{label} を抽出中...")

                            def run_one():
                                pt = core.extract_rows(
                                    srcs, topic_config, s_ns, e_ns,
                                    workers=int(extract_workers) or None,
                                    cache_dir=cache_dir)
                                if split_min:
                                    return core.write_csvs_split(
                                        pt, topic_config, outdir, veh, s_ns, e_ns,
                                        split_min, merged=merged_csv,
                                        merged_grid=merged_grid_sec, merged_hold=hold,
                                        drop_t_ns=drop_t_ns)
                                return core.write_csvs(
                                    pt, topic_config, outdir,
                                    core.window_base(veh, s_ns, e_ns),
                                    merged=merged_csv, merged_grid=merged_grid_sec,
                                    merged_hold=hold, drop_t_ns=drop_t_ns)
                            files_b, log2 = run_captured(run_one)
                            prog_b.progress(1.0, text=f"{label}: {len(files_b)} ファイル出力")
                            results.append((label, bool(files_b)))
                            with st.expander(f"ログ: {label}", expanded=False):
                                st.code(log1 + log2)
                        except Exception as e:
                            prog_b.progress(0.0, text=f"{label}: 失敗")
                            results.append((label, False))
                            st.error(f"{label}: {e}")
                    if cache_dir:
                        core.prune_cache(cache_dir, float(cache_max_gb))
                ok_n = sum(1 for _, ok in results if ok)
                (st.success if ok_n == len(results) else st.warning)(
                    f"バッチ完了: {ok_n}/{len(results)} 件成功。出力先: {outdir}")
                for label, ok in results:
                    st.write(("✅ " if ok else "❌ ") + label)
