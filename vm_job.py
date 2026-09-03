#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VM 抽出ジョブを UI (Streamlit) から切り離して実行・追跡するためのユーティリティ。

Streamlit のセッションはブラウザとの接続が切れると失われる (タブのスリープ・
再読み込み・PC スリープで画面が初期状態に戻る)。抽出ジョブ本体をここで
別プロセスとして起動し、ログと終了コードをファイルに落としておくことで、
画面が初期状態に戻っても「ジョブは生きている・再接続して回収できる」を保証する。

ジョブの実体は <outdir>/_vm_job/ 以下のファイル:
    job.json   起動パラメータ (run_detached.py への指示書)
    info.json  表示用メタ (ラベル・開始時刻)
    run.log    コマンドの stdout/stderr (追記)
    pid.txt    実コマンドの PID (生存確認用)
    exit.txt   終了コード (これが出来たら終了)
"""

import json
import os
import shutil
import subprocess
import sys
import time

JOB_DIRNAME = "_vm_job"
# exit が無いままログが伸びず、プロセスも見つからない状態がこの秒数続いたら
# 「ジョブ喪失」として追跡を打ち切る (ラッパごと強制終了された場合の保険)
STALE_TIMEOUT_SEC = 30.0


def job_paths(outdir):
    d = os.path.join(outdir, JOB_DIRNAME)
    return {
        "dir": d,
        "meta": os.path.join(d, "job.json"),
        "info": os.path.join(d, "info.json"),
        "log": os.path.join(d, "run.log"),
        "pid": os.path.join(d, "pid.txt"),
        "exit": os.path.join(d, "exit.txt"),
    }


def start_job(cmd, outdir, label, cwd):
    """コマンドを UI から独立したプロセスとして起動する (戻りは待たない)。

    cmd はリスト (shell 経由なし) または文字列 (shell 経由)。ラッパ
    (scripts/run_detached.py) を新しいプロセスグループ/セッションで起動する
    ことで、Streamlit 本体が落ちてもジョブは走り続ける。
    """
    paths = job_paths(outdir)
    shutil.rmtree(paths["dir"], ignore_errors=True)
    os.makedirs(paths["dir"], exist_ok=True)
    with open(paths["meta"], "w", encoding="utf-8") as f:
        json.dump({"cmd": cmd, "shell": isinstance(cmd, str), "cwd": cwd,
                   "log": paths["log"], "pid": paths["pid"],
                   "exit": paths["exit"]}, f, ensure_ascii=False)
    with open(paths["info"], "w", encoding="utf-8") as f:
        json.dump({"label": label,
                   "started_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                  f, ensure_ascii=False)
    wrapper = os.path.join(cwd, "scripts", "run_detached.py")
    kwargs = {}
    if os.name == "nt":
        # 新しいプロセスグループ + ウィンドウなし。親 (Streamlit) の終了に巻き込まれない
        kwargs["creationflags"] = (subprocess.CREATE_NO_WINDOW
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, wrapper, paths["meta"]],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, **kwargs)
    return paths


def load_job(outdir):
    """未回収のジョブがあれば表示用の情報を返す。無ければ None。

    戻り値: {"label", "started_at", "finished", "exit_code"}
    """
    paths = job_paths(outdir)
    if not os.path.exists(paths["info"]):
        return None
    try:
        with open(paths["info"], encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    info["finished"] = os.path.exists(paths["exit"])
    info["exit_code"] = _read_exit(paths) if info["finished"] else None
    return info


def _read_exit(paths):
    try:
        with open(paths["exit"], encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return -1


def _pid_alive(pid):
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            r = subprocess.run(f'tasklist /FI "PID eq {pid}" /NH', shell=True,
                               capture_output=True, text=True, timeout=15)
            return str(pid) in r.stdout
        except Exception:
            return True  # 確認できないときは生存扱い (誤って打ち切らない)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pid(paths):
    try:
        with open(paths["pid"], encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def read_log(outdir):
    paths = job_paths(outdir)
    try:
        with open(paths["log"], "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def tail_job(outdir, on_update, poll_sec=1.0, ui_interval_sec=0.5):
    """ジョブの終了までログを追いかける。戻り値: (終了コード, 全ログ文字列)。

    on_update(text) は表示更新用コールバック (ui_interval_sec ごとに間引いて呼ぶ。
    毎行更新すると WebSocket 転送と描画が膨らみ、タブが重くなるため)。
    exit.txt が出来たら正常終了。exit.txt が無いままプロセスが消え、ログも
    伸びなくなった場合は打ち切って -1 を返す (ラッパごと殺された場合の保険)。
    """
    paths = job_paths(outdir)
    last_ui = 0.0
    last_growth = time.monotonic()
    last_size = -1
    text = ""
    while True:
        text = read_log(outdir)
        if len(text) != last_size:
            last_size = len(text)
            last_growth = time.monotonic()
        now = time.monotonic()
        if now - last_ui >= ui_interval_sec:
            on_update(text)
            last_ui = now
        if os.path.exists(paths["exit"]):
            break
        if (now - last_growth > STALE_TIMEOUT_SEC
                and not _pid_alive(_read_pid(paths))):
            on_update(text + "\n[error] ジョブのプロセスが見つかりません "
                             "(ログの更新も止まっているため打ち切ります)")
            return -1, text
        time.sleep(poll_sec)
    on_update(text)
    return _read_exit(paths), text


def clear_job(outdir):
    """回収済みジョブの記録を消す (次回の「未回収ジョブ」表示を出さないため)。"""
    shutil.rmtree(job_paths(outdir)["dir"], ignore_errors=True)
