#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""出力済み CSV から指定列を一括で除去する後処理ツール。

抽出をやり直さずに、フォルダ内の全 CSV から列 (既定: t_ns) を取り除く。
GetMcapToCsv の出力 (UTF-8 BOM 付き) を同じ形式のまま書き戻すので、
Excel での開き方や他の列・行はいっさい変わらない。

使い方 (PowerShell / bash 共通):
    python drop_csv_column.py out                    # out/ 内の全 CSV から t_ns を除去
    python drop_csv_column.py out --dry-run          # 何が変わるかの確認だけ (書き換えない)
    python drop_csv_column.py out --columns t_ns t_sec   # 複数列を除去
    python drop_csv_column.py out\\GIGA09_*.csv       # ファイル・glob 指定も可

- 対象列が無い CSV はそのままスキップする (二重に実行しても安全)
- 書き換えは一時ファイル経由で行い、途中で失敗しても元ファイルは壊れない
- Excel などで開いたままの CSV は書き換えられないため、スキップして報告する

⚠ GetDruidUser に取り込む CSV では t_ns 列が必須。取り込み用のフォルダには使わないこと。
"""

import argparse
import csv
import glob
import os
import sys
import tempfile
import time


def iter_csv_paths(targets):
    """引数 (ファイル / フォルダ / glob) を CSV パスのリストに展開する。"""
    paths = []
    for t in targets:
        if os.path.isdir(t):
            paths.extend(sorted(glob.glob(os.path.join(t, "*.csv"))))
        elif os.path.isfile(t):
            paths.append(t)
        else:
            hits = sorted(glob.glob(t))
            if not hits:
                print(f"[warn] 見つかりません: {t}")
            paths.extend(p for p in hits if p.lower().endswith(".csv"))
    # 重複除去 (順序維持)
    seen = set()
    out = []
    for p in paths:
        key = os.path.abspath(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _replace_with_retry(tmp, path, attempts=5, wait_sec=0.5):
    """tmp で path を置き換える。Windows ではウイルススキャンや OneDrive 同期が
    書き終わった直後のファイルを一瞬つかむことがあるため、少し待って再試行する。"""
    for i in range(attempts):
        try:
            os.replace(tmp, path)  # 同一フォルダ内なのでアトミックに置き換わる
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(wait_sec)


def drop_columns(path, columns, dry_run):
    """1 つの CSV から指定列を除いて書き戻す。戻り値: 状態文字列。"""
    # utf-8-sig: BOM 付き (GetMcapToCsv の出力) も無しも読める。書き戻しは BOM 付きに統一
    tmp = None
    try:
        with open(path, "r", newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if header is None:
                return "空ファイルのためスキップ"
            hits = [i for i, name in enumerate(header) if name in columns]
            if not hits:
                return "対象列なし (変更不要)"
            removed = ", ".join(header[i] for i in hits)
            if dry_run:
                return f"列 {removed} を除去します (dry-run)"
            keep = [i for i in range(len(header)) if i not in hits]
            # 同じフォルダに一時ファイルを作って書き、最後に置き換える (途中失敗でも元は無傷)
            fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".",
                                       suffix=".tmp", dir=os.path.dirname(path) or ".")
            with os.fdopen(fd, "w", newline="", encoding="utf-8-sig") as g:
                writer = csv.writer(g)
                writer.writerow([header[i] for i in keep])
                for row in reader:
                    # 稀に列数が足りない行があっても落とさない (ある分だけ残す)
                    writer.writerow([row[i] for i in keep if i < len(row)])
        # 置き換えは元ファイルを閉じてから行う。Windows は開いているファイルを
        # 置き換えられない (全ファイルが WinError 5 になる) ため、with の外に置く
        _replace_with_retry(tmp, path)
        tmp = None  # 置き換え成功 = 一時ファイルは消滅済み
        return f"列 {removed} を除去しました"
    finally:
        if tmp is not None:
            try:
                os.remove(tmp)
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(
        description="出力済み CSV から指定列 (既定: t_ns) を一括除去する。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例: python drop_csv_column.py out\n"
               "    python drop_csv_column.py out --dry-run\n"
               "⚠ GetDruidUser 取り込み用の CSV では t_ns が必須のため実行しないこと。")
    parser.add_argument("targets", nargs="+", metavar="PATH",
                        help="CSV ファイル、フォルダ (直下の *.csv)、または glob パターン")
    parser.add_argument("--columns", nargs="+", default=["t_ns"], metavar="COL",
                        help="除去する列名 (既定: t_ns)")
    parser.add_argument("--dry-run", action="store_true",
                        help="書き換えずに、何が変わるかだけ表示する")
    args = parser.parse_args()

    paths = iter_csv_paths(args.targets)
    if not paths:
        print("[error] 対象の CSV がありません。パスを確認してください。")
        return 1

    columns = set(args.columns)
    print(f"[info] 対象 {len(paths)} ファイル / 除去する列: {', '.join(sorted(columns))}"
          + (" (dry-run: 書き換えなし)" if args.dry_run else ""))
    n_changed = n_skipped = n_failed = 0
    for p in paths:
        try:
            result = drop_columns(p, columns, args.dry_run)
        except OSError as e:
            # Windows でファイルがつかまれている場合など (Excel で開いたまま、
            # OneDrive 同期中、読み取り専用属性)。リトライ済みでもダメなら報告する
            print(f"[warn] {os.path.basename(p)}: 書き換えできません ({e})。"
                  "開いているアプリを閉じる・OneDrive 同期を一時停止する・"
                  "読み取り専用属性を外す、のいずれかを試して再実行してください。")
            n_failed += 1
            continue
        if "除去" in result:
            n_changed += 1
        else:
            n_skipped += 1
        print(f"[{'plan' if args.dry_run else 'ok'}] {os.path.basename(p)}: {result}")
    print(f"\n[done] {'変更予定' if args.dry_run else '変更'} {n_changed} 件 / "
          f"変更不要 {n_skipped} 件 / 失敗 {n_failed} 件")
    return 1 if n_failed else 0


if __name__ == "__main__":
    sys.exit(main())
