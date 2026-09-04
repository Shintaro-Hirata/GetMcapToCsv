#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vm_job.start_job から起動されるラッパ。指定コマンドを実行し、
stdout/stderr をログファイルへ、終了コードを exit ファイルへ書く。

Streamlit (UI) 本体とは独立したプロセスとして動くため、画面のセッションが
切れてもコマンドは最後まで実行される。単体では次のように使う:
    python scripts/run_detached.py <job.json のパス>
job.json: {"cmd": [...] or "...", "shell": bool, "cwd": str,
           "log": path, "pid": path, "exit": path}
"""

import json
import os
import subprocess
import sys


def main():
    with open(sys.argv[1], encoding="utf-8") as f:
        meta = json.load(f)
    # Python 側のバッファリングを避けるため、ログはコマンドに直結する
    rc = 1
    with open(meta["log"], "ab", buffering=0) as log:
        try:
            env = dict(os.environ)
            env.setdefault("PYTHONUNBUFFERED", "1")
            proc = subprocess.Popen(
                meta["cmd"], cwd=meta.get("cwd"), shell=bool(meta.get("shell")),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env=env)
            with open(meta["pid"], "w", encoding="utf-8") as f:
                f.write(str(proc.pid))
            rc = proc.wait()
        except Exception as e:  # 起動自体の失敗もログと exit に残す
            log.write(f"[error] コマンドを起動できませんでした: {e}\n".encode("utf-8"))
            rc = 1
        finally:
            with open(meta["exit"], "w", encoding="utf-8") as f:
                f.write(str(rc))
    return rc


if __name__ == "__main__":
    sys.exit(main())
