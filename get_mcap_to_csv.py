#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GCS バケット (t2-ft-original-data) 上の走行データ mcap から、
指定 vehicle ID と指定時間帯のメッセージを CSV に抽出するツール。

バケット構造 (zero-plotter の運用と同じ):
    t2-ft-original-data/
      20251204_GIGA07__xxxx/                 <- YYYYMMDD_<vehicle>__...
        recording/
          20251204_120023_GIGA07_xxxx_yyy/   <- YYYYMMDD_HHMMSS_<vehicle>_...
            record_develop/
              record_develop_0.mcap ...

使い方の例:
    # 認証 (初回のみ)
    gcloud auth application-default login

    # 対象になる mcap ファイルの一覧だけ確認
    python get_mcap_to_csv.py --vehicle GIGA07 --start "2025-12-04 12:00" --end "2025-12-04 12:10" --list-only

    # 含まれるトピック一覧を確認 (フィールド名調査用)
    python get_mcap_to_csv.py --vehicle GIGA07 --start "2025-12-04 12:00" --end "2025-12-04 12:10" --list-topics

    # CSV 抽出 (トピック設定は JSON で指定)
    python get_mcap_to_csv.py --vehicle GIGA07 --start "2025-12-04 12:00" --end "2025-12-04 12:10" \
        --topics topics.example.t2.json --outdir out

    # ローカルの mcap を処理 (GCS を使わない)
    python get_mcap_to_csv.py --local "*.mcap" --topics topics.example.apollo.json

注意:
    バケット内の mcap は ros2idl エンコードの /t2/* トピックが主。
    デコードには mcap-ros2idl-support (zero-plotter リポジトリ内の社内パッケージ) が必要。
    requirements.txt / README.md を参照。
"""

import argparse
import csv
import datetime
import glob
import json
import os
import re
import shutil
import tempfile
import time
import threading
from collections import OrderedDict, defaultdict
from concurrent.futures import (ProcessPoolExecutor, ThreadPoolExecutor,
                                as_completed, wait)
from fnmatch import fnmatch

from mcap.reader import make_reader  # make_reader

JST = datetime.timezone(datetime.timedelta(hours=9))

DEFAULT_BUCKET = "t2-ft-original-data"
DEFAULT_SUBDIR = "record_develop"

# トピック設定のデフォルト (参考にした extract.py と同じ /apollo 用)。
# 実際のバケットデータは /t2/* トピックのため、通常は --topics で JSON を指定する。
DEFAULT_TOPIC_CONFIG = {
    "/apollo/canbus/chassis": {
        "suffix": "chassis",
        "fields": [
            "speed_mps",
            "brake_percentage",
            "throttle_percentage",
            "steering_percentage",
            "steering_torque_nm",
        ],
    },
    "/apollo/control": {
        "suffix": "control",
        "fields": [
            "debug.simple_lateral_debug.lateral_error",
            "debug.simple_lateral_debug.heading_error",
            "debug.simple_lateral_debug.steer_angle",
        ],
    },
}

# フラット展開時にリストを何要素まで展開するか
FLATTEN_MAX_LIST = 8

# GCS egress (東京リージョン→インターネット) の概算単価。コスト表示にのみ使用
EGRESS_USD_PER_GB = 0.12
USD_JPY = 150.0

DEFAULT_CACHE_DIR = "mcap_cache"
DEFAULT_CACHE_MAX_GB = 20.0


# ------------------------------------------------------------------
# GCS 転送量の計測 (コスト見える化) とローカルキャッシュ
# ------------------------------------------------------------------
class TransferStats:
    """GCS から実際に読んだバイト数と、キャッシュで節約したバイト数を数える。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.gcs_bytes = 0
        self.cache_bytes = 0

    def reset(self):
        with self._lock:
            self.gcs_bytes = 0
            self.cache_bytes = 0

    def add_gcs(self, n):
        with self._lock:
            self.gcs_bytes += int(n)

    def add_cache(self, n):
        with self._lock:
            self.cache_bytes += int(n)

    def snapshot(self):
        with self._lock:
            return self.gcs_bytes, self.cache_bytes


STATS = TransferStats()  # プロセス内の合計 (ワーカープロセス分は戻り値経由で合算)


def cost_str(n_bytes):
    """バイト数から egress 費用の概算表示を作る。"""
    gb = n_bytes / (1024 ** 3)
    usd = gb * EGRESS_USD_PER_GB
    return f"約 ¥{usd * USD_JPY:,.0f} (${usd:.2f})"


class CountingFile:
    """read したバイト数を TransferStats に加算するファイルラッパ。"""

    def __init__(self, f, stats):
        self._f = f
        self._stats = stats

    def read(self, n=-1):
        data = self._f.read(n)
        if data:
            self._stats.add_gcs(len(data))
        return data

    def __getattr__(self, name):
        return getattr(self._f, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._f.close()
        return False


def cache_file_path(cache_dir, bucket_name, blob_name):
    """blob に対応するキャッシュファイルのパス。"""
    safe = blob_name.replace("\\", "/").lstrip("/").replace("..", "__")
    return os.path.join(cache_dir, bucket_name, *safe.split("/"))


def cache_lookup(cache_dir, bucket_name, blob_name, expected_size):
    """キャッシュにサイズ一致のファイルがあればそのパスを返す。"""
    if not cache_dir:
        return None
    path = cache_file_path(cache_dir, bucket_name, blob_name)
    try:
        if os.path.getsize(path) == expected_size:
            os.utime(path, None)  # プルーニング (古い順削除) 用に参照時刻を更新
            return path
    except OSError:
        pass
    return None


def cache_store_download(blob, cache_dir, stats=None):
    """blob をキャッシュへダウンロードし、キャッシュ内のパスを返す (temp→rename で原子的に)。"""
    path = cache_file_path(cache_dir, blob.bucket.name, blob.name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    blob.download_to_filename(tmp)
    os.replace(tmp, path)
    if stats is not None:
        stats.add_gcs(os.path.getsize(path))
    return path


def prune_cache(cache_dir, max_gb):
    """キャッシュ合計サイズが上限を超えていたら、参照が古いファイルから削除する。"""
    if not cache_dir or not os.path.isdir(cache_dir):
        return
    entries = []
    for root, _, names in os.walk(cache_dir):
        for name in names:
            p = os.path.join(root, name)
            try:
                st = os.stat(p)
                entries.append((st.st_mtime, st.st_size, p))
            except OSError:
                continue
    total = sum(sz for _, sz, _ in entries)
    limit = max_gb * (1024 ** 3)
    if total <= limit:
        return
    for _, sz, p in sorted(entries):
        try:
            os.remove(p)
            total -= sz
        except OSError:
            continue
        if total <= limit:
            break
    print(f"[info] キャッシュを {size_str(total)} まで削減しました (上限 {max_gb:.0f}GB)")


def cache_total_size(cache_dir):
    """キャッシュディレクトリの合計サイズ (バイト)。"""
    total = 0
    if cache_dir and os.path.isdir(cache_dir):
        for root, _, names in os.walk(cache_dir):
            for name in names:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
    return total


# ------------------------------------------------------------------
# デコーダ (入っているものを全部使う)
# ------------------------------------------------------------------
def _apex_json_capable(factory_cls):
    """インストール済み mcap-ros2idl-support が apex_json エンコードを解けるか。

    古い版は decode_factory が apex_json を分岐せず "Unknown schema encoding" になる。
    schema 構築ロジックのソースに "apex_json" が現れるかで判定する。
    判定不能なら None (未知として警告は出さない)。
    """
    try:
        import inspect
        src = inspect.getsource(factory_cls._build_reader)
        return "apex_json" in src
    except Exception:
        return None


def build_decoder_factories(quiet=False):
    """利用可能な mcap デコーダファクトリを集める。"""
    factories = []
    names = []

    try:
        from mcap_ros2idl_support import Ros2DecodeFactory  # ros2idl (/t2 トピック用)
        factories.append(Ros2DecodeFactory())
        names.append("ros2idl (mcap-ros2idl-support)")
        if not quiet and _apex_json_capable(Ros2DecodeFactory) is False:
            print("[warn] mcap-ros2idl-support が古く apex_json 非対応です。"
                  " apex_json のトピック (例: /t2/main_mabx/*, /t2/control/demand*) は"
                  " デコードできず 0 行になります。zero-plotter を最新 (yatagarasu/main) に"
                  " 更新してください: cd zero-plotter && git pull")
    except ImportError:
        if not quiet:
            print("[warn] mcap-ros2idl-support が見つかりません。"
                  " /t2/* トピック (ros2idl) はデコードできません。README.md を参照。")

    try:
        from mcap_protobuf.decoder import DecoderFactory as PbDecoderFactory  # protobuf 用
        factories.append(PbDecoderFactory())
        names.append("protobuf (mcap-protobuf-support)")
    except ImportError:
        if not quiet:
            print("[warn] mcap-protobuf-support が見つかりません。"
                  " /apollo/* トピック (protobuf) はデコードできません。")

    try:
        from mcap_ros2.decoder import DecoderFactory as Ros2MsgDecoderFactory  # ros2msg 用
        factories.append(Ros2MsgDecoderFactory())
        names.append("ros2msg (mcap-ros2-support)")
    except ImportError:
        pass  # 任意

    if factories and not quiet:
        print(f"[info] decoders: {', '.join(names)}")
    return factories


# ------------------------------------------------------------------
# 時刻
# ------------------------------------------------------------------
TIME_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y%m%d_%H%M%S",
    "%Y%m%d%H%M%S",
]


def parse_jst(text):
    """JST のローカル時刻文字列を aware datetime にする。"""
    for fmt in TIME_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt).replace(tzinfo=JST)
        except ValueError:
            continue
    raise SystemExit(f"[error] 時刻を解釈できません: {text!r} "
                     f"(例: \"2025-12-04 12:00\" / \"2025/12/04 12:00:00\")")


def to_ns(dt):
    return int(dt.timestamp() * 1e9)


def fmt_jst(t_ns):
    dt = datetime.datetime.fromtimestamp(t_ns / 1e9, JST)
    return dt.strftime("%m/%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


# ------------------------------------------------------------------
# GCS 上の mcap 探索
# ------------------------------------------------------------------
def gcs_client():
    try:
        from google.cloud import storage  # storage.Client
    except ImportError:
        raise SystemExit("[error] google-cloud-storage が入っていません。"
                         " `pip install -r requirements.txt` を実行してください。")
    try:
        return storage.Client()
    except Exception as e:
        raise SystemExit(
            "[error] GCP 認証に失敗しました。以下を実行してから再試行してください:\n"
            "    gcloud auth application-default login\n"
            f"詳細: {e}")


def list_top_dirs(client, bucket_name, vehicle, start_dt, end_dt):
    """時間帯にかかる可能性のあるトップレベルディレクトリを列挙。

    ディレクトリ名は「運行終了日_車両名__SSD名」(例: 20260702_GIGA09__SSD0097-FOT)。
    日付は運行**終了**日なので、夜間走行は翌日付のディレクトリに入る。
    そのため抽出開始日から抽出終了日+1日までを候補にする。
    """
    dates = []
    d = start_dt.date()
    while d <= end_dt.date() + datetime.timedelta(days=1):
        dates.append(d)
        d += datetime.timedelta(days=1)

    pat = re.compile(rf"^\d{{8}}_{re.escape(vehicle)}(?:__.*)?/$")
    top_dirs = []
    for date in dates:
        prefix = f"{date:%Y%m%d}_"
        it = client.list_blobs(bucket_name, prefix=prefix, delimiter="/")
        for _ in it:  # prefixes を得るにはページを消費する必要がある
            pass
        for p in sorted(it.prefixes):
            if pat.match(p):
                top_dirs.append(p)
    return top_dirs


def parse_session_start(session_name):
    """recording セッションディレクトリ名 (YYYYMMDD_HHMMSS_...) の開始時刻 (JST)。"""
    m = re.match(r"^(\d{8})_(\d{6})_", session_name)
    if not m:
        return None
    try:
        return datetime.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=JST)
    except ValueError:
        return None


def list_subdirs(client, bucket_name, prefix):
    """prefix 直下のサブディレクトリ (末尾 / 付きプレフィックス) を列挙する。"""
    it = client.list_blobs(bucket_name, prefix=prefix, delimiter="/")
    for _ in it:  # prefixes を得るにはページを消費する必要がある
        pass
    return sorted(it.prefixes)


def list_candidate_blobs(client, bucket_name, top_dirs, subdir, start_dt, end_dt,
                         lookback_hours=24):
    """トップレベルディレクトリ配下の mcap blob を列挙する。

    まず recording/ 直下のセッションディレクトリ名 (YYYYMMDD_HHMMSS_...) だけを
    一覧し、開始時刻が [抽出開始 - lookback_hours, 抽出終了] に入るセッションのみ
    中身 (指定 subdir) を listing する。時間帯外のセッションや record_develop 以外の
    サブディレクトリはオブジェクト一覧すら取得しない。
    recording/ 階層がない場合は直下の <subdir>/*.mcap も探す。
    """
    earliest = start_dt - datetime.timedelta(hours=lookback_hours)
    blobs = []
    n_skipped = 0
    for top in top_dirs:
        found_here = []
        for session_prefix in list_subdirs(client, bucket_name, f"{top}recording/"):
            session_name = session_prefix[len(top) + len("recording/"):].strip("/")
            session_start = parse_session_start(session_name)
            if session_start is not None and not (earliest <= session_start <= end_dt):
                n_skipped += 1
                continue
            if session_start is None:
                print(f"[info] セッション名から時刻を読めないため中身を確認: {session_prefix}")
            else:
                print(f"[info] セッション採用: {session_prefix}  (開始 {session_start:%m/%d %H:%M})")
            scan_prefix = session_prefix if subdir == "*" else f"{session_prefix}{subdir}/"
            for blob in client.list_blobs(bucket_name, prefix=scan_prefix):
                if blob.name.endswith(".mcap"):
                    found_here.append(blob)
        # recording/ 階層を使わない旧レイアウトへのフォールバック
        if subdir != "*":
            for blob in client.list_blobs(bucket_name, prefix=f"{top}{subdir}/"):
                if blob.name.endswith(".mcap"):
                    found_here.append(blob)
        blobs.extend(sorted(found_here, key=lambda b: b.name))
    if n_skipped:
        print(f"[info] 時間帯外のセッション {n_skipped} 件を除外 "
              f"(開始時刻が {earliest:%m/%d %H:%M} より前または {end_dt:%m/%d %H:%M} より後)")
    return blobs


class GcsMcapSource:
    """GCS blob を mcap 入力として扱う。open() は seek 可能なファイルオブジェクトを返す。"""

    def __init__(self, blob):
        self.blob = blob
        self.name = f"gs://{blob.bucket.name}/{blob.name}"
        self.size = blob.size
        # 同じディレクトリ = 同じ録画セッション。連番二分探索のグループ単位に使う
        self.session = blob.name.rsplit("/", 1)[0]
        self.time_range = None  # 絞り込み時に判明したメッセージ時刻範囲 (不明なら None)

    def open(self, chunk_size=16 * 1024 * 1024):
        # 読んだバイト数を計測する (転送量とコストの見える化)
        return CountingFile(self.blob.open("rb", chunk_size=chunk_size), STATS)


class LocalMcapSource:
    def __init__(self, path):
        self.path = path
        self.name = path
        self.size = os.path.getsize(path)
        self.session = None  # ローカルは連番の保証がないので個別チェック
        self.time_range = None

    def open(self, chunk_size=None):
        return open(self.path, "rb")


def read_time_range(source):
    """mcap のサマリだけを読んでメッセージの (start_ns, end_ns) を返す。無ければ None。"""
    try:
        with source.open(chunk_size=256 * 1024) as f:
            reader = make_reader(f)
            summary = reader.get_summary()
            stats = summary.statistics if summary else None
            if stats and stats.message_count > 0:
                return stats.message_start_time, stats.message_end_time
    except Exception as e:
        print(f"[warn] サマリ読み込み失敗 ({source.name}): {e}")
    return None


_NUMBER_SUFFIX = re.compile(r"_(\d+)\.mcap$")


def numeric_suffix(name):
    """record_develop_16.mcap のような連番付きファイル名から番号を取り出す。"""
    m = _NUMBER_SUFFIX.search(name)
    return int(m.group(1)) if m else None


def _overlaps(rng, start_ns, end_ns):
    """時刻範囲が重なるか。サマリが読めなかったファイル (None) は念のため対象扱い。"""
    if rng is None:
        return True
    return rng[0] <= end_ns and rng[1] >= start_ns


def _scan_group(files, start_ns, end_ns, cache):
    """グループ内の全ファイルのサマリを読んで重なるものを返す。"""
    selected = []
    for i, src in enumerate(files):
        if i not in cache:
            cache[i] = read_time_range(src)
        if _overlaps(cache[i], start_ns, end_ns):
            selected.append((src, cache[i]))
    return selected


def _bisect_group(files, start_ns, end_ns, cache):
    """連番順 = 時刻順のグループから、二分探索で重なる範囲 [left, right] を求める。

    サマリを読めないファイルに当たった場合は全件スキャンにフォールバックする。
    """
    def rng(i):
        if i not in cache:
            cache[i] = read_time_range(files[i])
        if cache[i] is None:
            raise LookupError(i)
        return cache[i]

    n = len(files)
    try:
        # 左端: end_time >= start_ns となる最初のファイル
        lo, hi, left = 0, n - 1, n
        while lo <= hi:
            mid = (lo + hi) // 2
            if rng(mid)[1] >= start_ns:
                left, hi = mid, mid - 1
            else:
                lo = mid + 1
        if left == n:
            return []
        # 右端: start_time <= end_ns となる最後のファイル
        lo, hi, right = left, n - 1, left - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if rng(mid)[0] <= end_ns:
                right, lo = mid, mid + 1
            else:
                hi = mid - 1
        # 境界の内側は読まずに時刻連続性を信頼して選択する
        return [(files[i], cache.get(i)) for i in range(left, right + 1)]
    except LookupError:
        return _scan_group(files, start_ns, end_ns, cache)


def _select_in_group(files, start_ns, end_ns):
    """1 グループ (= 1 セッションディレクトリ) から対象ファイルを選ぶ。

    戻り値: (選ばれた (source, 時刻範囲 or None) のリスト, サマリ読み込み回数)
    """
    cache = {}
    nums = [numeric_suffix(f.name) for f in files]
    sequential = (len(files) >= 4 and all(x is not None for x in nums)
                  and len(set(nums)) == len(nums))
    if sequential:
        files = [f for _, f in sorted(zip(nums, files), key=lambda p: p[0])]
        selected = _bisect_group(files, start_ns, end_ns, cache)
    else:
        selected = _scan_group(files, start_ns, end_ns, cache)
    return selected, len(cache)


def filter_sources_by_time(sources, start_ns, end_ns, workers=16):
    """時間帯と重なるファイルだけに絞る。

    セッションディレクトリごとにグループ化し、連番ファイルは二分探索で境界のみ
    サマリを読む。グループ間は並列で処理する。
    """
    groups = OrderedDict()
    for src in sources:
        key = src.session if src.session else src.name  # セッション不明なら単独グループ
        groups.setdefault(key, []).append(src)

    selected = []
    total_reads = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(_select_in_group, files, start_ns, end_ns): key
                   for key, files in groups.items()}
        for fut in as_completed(futures):
            try:
                picked, reads = fut.result()
            except Exception as e:
                print(f"[warn] 絞り込み失敗 ({futures[fut]}): {e}")
                continue
            total_reads += reads
            for src, rng in picked:
                src.time_range = rng  # 抽出フェーズでダウンロード方式の判断に使う
                selected.append(src)

    # 二分探索の内側で時刻を読まずに確定したファイルは、表示用に実時刻を追加取得する
    # (選定後の少数ファイルだけなので軽い)
    missing = [s for s in selected if s.time_range is None]
    if missing:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(missing)))) as pool:
            for src, rng in zip(missing, pool.map(read_time_range, missing)):
                src.time_range = rng
                total_reads += 1

    selected.sort(key=lambda s: s.name)
    for src in selected:
        rng = src.time_range
        if rng is not None:
            print(f"[info] 対象: {src.name}  "
                  f"({fmt_jst(rng[0])} - {fmt_jst(rng[1])}, {size_str(src.size)})")
        else:
            print(f"[info] 対象: {src.name}  ({size_str(src.size)})")
    print(f"[info] 絞り込み完了: {len(sources)} 件中 {len(selected)} 件が対象 "
          f"(メタデータ読み込み {total_reads} 回)")
    return selected


def find_sibling_files(client, bucket_name, selected_sources, subdir):
    """選ばれた record_develop ファイルと同じ連番の兄弟サブディレクトリのファイルを追加する。

    develop / image / sensor は同時刻で分割されているため、develop 側の絞り込みで
    確定した連番をそのまま流用し、兄弟側のサマリ読み込みは行わない。
    時刻範囲も develop 側のものを引き継ぎ、ダウンロード方式の判断に使う。
    """
    sibling_sources = []
    by_dir = defaultdict(list)
    for src in selected_sources:
        if isinstance(src, GcsMcapSource):
            by_dir[src.session].append(src)
    for dev_dir, srcs in sorted(by_dir.items()):
        parent = dev_dir.rsplit("/", 1)[0]  # セッションディレクトリ
        prefix = f"{parent}/{subdir}/"
        wanted = {}
        for s in srcs:
            idx = numeric_suffix(s.name)
            if idx is not None:
                wanted[idx] = s
            else:
                print(f"[warn] 連番が読めないため {subdir} の対応付け不可: {s.name}")
        if not wanted:
            continue
        found = {}
        for blob in client.list_blobs(bucket_name, prefix=prefix):
            if blob.name.endswith(".mcap"):
                idx = numeric_suffix(blob.name)
                if idx in wanted:
                    found[idx] = blob
        if not found:
            subs = list_subdirs(client, bucket_name, f"{parent}/")
            names = [s[len(parent) + 1:].strip("/") for s in subs]
            print(f"[warn] {prefix} に mcap がありません。"
                  f"このセッションのサブディレクトリ: {', '.join(names) or '(なし)'}")
        for idx in sorted(wanted):
            if idx in found:
                sib = GcsMcapSource(found[idx])
                sib.time_range = wanted[idx].time_range
                sibling_sources.append(sib)
                print(f"[info] {subdir} 追加: {sib.name}  ({size_str(sib.size)})")
            elif found:
                print(f"[warn] 対応する {subdir} ファイルがありません: "
                      f"{prefix} の連番 {idx}")
    return sibling_sources


def size_str(n):
    """サイズの表示用文字列。KB 以上は小数 1 桁 (進捗が分かるように)。"""
    if n is None:
        return "?"
    if n < 1024:
        return f"{n:.0f}B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
    return f"{n:.1f}TB"


# ------------------------------------------------------------------
# フィールド抽出
# ------------------------------------------------------------------
_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def dig(msg, path):
    """ドット区切り (配列添字 [i] も可) でネストしたフィールドを取り出す。無ければ空文字。

    ros2idl デコード結果 (dict) と protobuf デコード結果 (属性アクセス) の両対応。
    """
    o = msg
    for m in _PATH_TOKEN.finditer(path.lstrip(".")):
        name, index = m.group(1), m.group(2)
        if o is None:
            return ""
        if name is not None:
            if isinstance(o, dict):
                o = o.get(name)
            else:
                o = getattr(o, name, None)
        else:
            i = int(index)
            try:
                o = o[i]
            except (IndexError, KeyError, TypeError):
                return ""
    return "" if o is None else o


def flatten(obj, prefix="", out=None):
    """メッセージ全体をドット区切りキーの 1 段の dict に展開する。"""
    if out is None:
        out = OrderedDict()
    if isinstance(obj, dict):
        for k, v in obj.items():
            flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj[:FLATTEN_MAX_LIST]):
            flatten(v, f"{prefix}[{i}]", out)
        if len(obj) > FLATTEN_MAX_LIST:
            out[f"{prefix}.length"] = len(obj)
    elif isinstance(obj, (bytes, bytearray)):
        out[prefix] = f"<bytes:{len(obj)}>"
    elif isinstance(obj, (str, int, float, bool)) or obj is None:
        out[prefix] = obj
    elif hasattr(obj, "ListFields"):  # protobuf メッセージ
        for fd, v in obj.ListFields():
            flatten(v, f"{prefix}.{fd.name}" if prefix else fd.name, out)
    elif hasattr(obj, "__slots__") or hasattr(obj, "__dict__"):
        attrs = getattr(obj, "__slots__", None) or vars(obj).keys()
        for k in attrs:
            if str(k).startswith("_"):
                continue
            flatten(getattr(obj, k, None), f"{prefix}.{k}" if prefix else str(k), out)
    else:
        out[prefix] = str(obj)
    return out


# ------------------------------------------------------------------
# 抽出本体
# ------------------------------------------------------------------
def load_topic_config(path):
    if path is None:
        print("[info] --topics 未指定のためデフォルト設定 (/apollo 用) を使用します。"
              " バケットのデータは通常 /t2/* トピックなので、--list-topics で確認の上"
              " topics.example.t2.json を元に設定を作ることを推奨します。")
        return DEFAULT_TOPIC_CONFIG
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    config = {}
    for topic, entry in raw.items():
        if not topic.startswith("/"):  # "_comment" などの注記キーは無視
            continue
        if isinstance(entry, list):  # 簡略形: フィールドのリストだけ
            entry = {"fields": entry}
        suffix = entry.get("suffix") or topic.strip("/").replace("/", "_")
        config[topic] = {"suffix": suffix, "fields": list(entry.get("fields", []))}
        if entry.get("columns"):  # フラット展開時に残す列 (フル列名)
            config[topic]["columns"] = list(entry["columns"])
        if entry.get("rename"):  # CSV 出力時の列名変更 {元の列名: 出力名}
            config[topic]["rename"] = dict(entry["rename"])
    return config


def _match_any(topic, patterns):
    return any(fnmatch(topic, p) for p in patterns)


def _collect_rows(reader, topic_config, exclude_pats, start_ns, end_ns):
    """開いた mcap reader から行データを収集する。

    topic_config が None のときは全トピック対象。exclude_pats に一致するトピックは
    デコード自体をスキップする (対象トピックのリストを先に確定して reader に渡す)。
    設定の "columns" (フル列名リスト) があるトピックは、フラット展開後にその列だけ残す。
    """
    all_topics = topic_config is None
    include_cols = {}
    if not all_topics:
        for t, cfg in topic_config.items():
            if cfg.get("columns"):
                include_cols[t] = frozenset(cfg["columns"])
    if all_topics:
        topics_arg = None
        if exclude_pats:
            summary = reader.get_summary()
            if summary is not None:
                chans = sorted({ch.topic for ch in summary.channels.values()})
                topics_arg = [t for t in chans if not _match_any(t, exclude_pats)]
    else:
        topics_arg = [t for t in topic_config if not _match_any(t, exclude_pats)]

    per_topic = {} if all_topics else {t: [] for t in topic_config}
    decode_errors = defaultdict(int)
    count = 0
    it = reader.iter_decoded_messages(
        topics=topics_arg,
        start_time=start_ns,
        end_time=end_ns,
        log_time_order=True,
    )
    while True:
        try:
            schema, channel, message, decoded = next(it)
        except StopIteration:
            break
        except Exception as e:  # 個別メッセージのデコード失敗は数えて続行
            decode_errors[str(e)[:120]] += 1
            continue
        topic = channel.topic
        if exclude_pats and _match_any(topic, exclude_pats):
            continue  # サマリが読めず topics_arg で絞れなかった場合の保険
        if all_topics:
            fields = []
            if topic not in per_topic:
                per_topic[topic] = []
        else:
            fields = topic_config[topic]["fields"]
        r = {"t_ns": message.log_time}
        if fields:
            for fld in fields:
                r[fld.split(".")[-1]] = dig(decoded, fld)
        else:  # フィールド未指定ならメッセージ全体をフラット展開
            include = include_cols.get(topic)
            for k, v in flatten(decoded).items():
                if include is not None and k not in include:
                    continue
                r[k] = v
        per_topic[topic].append(r)
        count += 1
    return per_topic, dict(decode_errors), count


def _stream_saving_fraction(blob, topics, start_ns, end_ns, stats):
    """サマリだけ読み、チャンクスキップでどれだけ転送を減らせるか (0.0-1.0) を返す。"""
    try:
        with CountingFile(blob.open("rb", chunk_size=256 * 1024), stats) as f:
            summary = make_reader(f).get_summary()
        if summary is None or not summary.chunk_indexes:
            return None
        sel_ids = {cid for cid, ch in summary.channels.items() if ch.topic in topics}
        total = needed = 0
        for ci in summary.chunk_indexes:
            if start_ns is not None and ci.message_end_time < start_ns:
                continue
            if end_ns is not None and ci.message_start_time >= end_ns:
                continue
            total += ci.chunk_length + ci.message_index_length
            if any(cid in ci.message_index_offsets for cid in sel_ids):
                needed += ci.chunk_length  # ストリーミングが読むのはチャンク本体のみ
        if total <= 0:
            return None
        return 1.0 - needed / total
    except Exception:
        return None


# 一括ダウンロードをやめて部分読み込みに切り替える削減率のしきい値
_AUTO_STREAM_SAVING = 0.4


def _extract_worker(job):
    """1 ファイル分のダウンロード + デコードを行うワーカー (別プロセスで実行可)。

    戻り値: (表示名, per_topic, decode_errors, 行数, 秒数, GCS読込バイト, キャッシュ利用バイト)
    """
    t0 = time.monotonic()
    factories = build_decoder_factories(quiet=True)
    stats = TransferStats()  # ワーカープロセス内のローカル計測 (戻り値で親へ返す)
    tmpdir = None
    cache_dir = job.get("cache_dir")
    try:
        if job["kind"] == "gcs":
            cached = cache_lookup(cache_dir, job["bucket"], job["blob_name"], job.get("size"))
            if cached:
                # キャッシュヒット時は GCS に一切アクセスしない (認証も不要)
                stats.add_cache(os.path.getsize(cached))
                f = open(cached, "rb")
            else:
                from google.cloud import storage  # storage.Client
                blob = storage.Client().bucket(job["bucket"]).blob(job["blob_name"])
                download = job["download"]
                if download and job.get("topic_config") is not None:
                    # トピックが絞られているときはサマリで必要チャンク比を見て、
                    # 大幅に減るなら丸ごとダウンロードをやめて部分読み込みにする
                    saving = _stream_saving_fraction(
                        blob, set(job["topic_config"].keys()),
                        job["start_ns"], job["end_ns"], stats)
                    if saving is not None and saving >= _AUTO_STREAM_SAVING:
                        download = False
                        print(f"[info] {job['name'].rsplit('/', 1)[-1]}: "
                              f"選択トピックに不要なチャンクが {saving * 100:.0f}% → 部分読み込みに切替")
                if download:
                    # 時間帯がファイルの大半を占めるなら一括ダウンロードの方が速い
                    if cache_dir:
                        local = cache_store_download(blob, cache_dir, stats)
                    else:
                        tmpdir = tempfile.mkdtemp(prefix="getmcap_")
                        local = os.path.join(tmpdir, "part.mcap")
                        blob.download_to_filename(local)
                        stats.add_gcs(os.path.getsize(local))
                    f = open(local, "rb")
                else:
                    f = CountingFile(blob.open("rb", chunk_size=16 * 1024 * 1024), stats)
        else:
            f = open(job["path"], "rb")
        with f:
            reader = make_reader(f, decoder_factories=factories)
            per_topic, errors, count = _collect_rows(
                reader, job["topic_config"], job["exclude"],
                job["start_ns"], job["end_ns"])
        return (job["name"], per_topic, errors, count, time.monotonic() - t0,
                stats.gcs_bytes, stats.cache_bytes)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _window_coverage(src, start_ns, end_ns):
    """抽出時間帯がファイルの時刻範囲をどれだけ覆うか (0.0-1.0)。不明なら 1.0。"""
    rng = getattr(src, "time_range", None)
    if rng is None or rng[1] <= rng[0]:
        return 1.0  # 二分探索の内側 (= 全体が時間帯内) など
    overlap = min(rng[1], end_ns or rng[1]) - max(rng[0], start_ns or rng[0])
    return max(0.0, overlap / (rng[1] - rng[0]))


def build_extract_jobs(sources, topic_config, exclude_pats, start_ns, end_ns,
                       no_download=False, cache_dir=None):
    jobs = []
    for src in sources:
        job = {
            "name": src.name,
            "topic_config": topic_config,
            "exclude": exclude_pats or [],
            "start_ns": start_ns,
            "end_ns": end_ns,
            "cache_dir": cache_dir,
        }
        if isinstance(src, GcsMcapSource):
            job["kind"] = "gcs"
            job["bucket"] = src.blob.bucket.name
            job["blob_name"] = src.blob.name
            job["size"] = src.size
            # 時間帯がファイルの半分以上を覆うなら一括ダウンロード、
            # 一部だけならチャンクインデックスによる部分読み込み
            job["download"] = (not no_download
                               and _window_coverage(src, start_ns, end_ns) >= 0.5)
        else:
            job["kind"] = "local"
            job["path"] = src.path
            job["download"] = False
        jobs.append(job)
    return jobs


def extract_rows(sources, topic_config, start_ns, end_ns, exclude_pats=None,
                 workers=None, no_download=False, progress=None, cache_dir=None):
    """全ソースから対象トピックの行データを収集する (ファイル単位で並列)。

    topic_config が None のときは mcap に含まれる全トピックを対象にする。
    progress: callable(完了ファイル数, 総ファイル数, 直近完了ファイル名)。
    cache_dir を指定すると一括ダウンロードをキャッシュし、再実行時は GCS を読まない。
    """
    jobs = build_extract_jobs(sources, topic_config, exclude_pats,
                              start_ns, end_ns, no_download, cache_dir)
    if workers is None:
        workers = min(len(jobs), max(1, (os.cpu_count() or 4) - 1), 8)
    workers = max(1, workers)

    n_dl = sum(1 for j in jobs if j.get("download"))
    print(f"[info] {len(jobs)} ファイルを読み込み (並列 {workers}, "
          f"一括ダウンロード {n_dl} 件 / 部分読み込み {len(jobs) - n_dl} 件)")

    all_topics = topic_config is None
    per_topic = {} if all_topics else {t: [] for t in topic_config}
    decode_errors = defaultdict(int)

    n_done = [0]

    def merge(result):
        name, part, errors, count, sec, gcs_b, cache_b = result
        STATS.add_gcs(gcs_b)
        STATS.add_cache(cache_b)
        for topic, rows in part.items():
            per_topic.setdefault(topic, []).extend(rows)
        for err, n in errors.items():
            decode_errors[err] += n
        src_note = " [キャッシュ]" if cache_b and not gcs_b else ""
        print(f"[info]   {name.rsplit('/', 1)[-1]}: {count} 行 ({sec:.1f} 秒){src_note}")
        n_done[0] += 1
        if progress:
            progress(n_done[0], len(jobs), name)

    if workers == 1 or len(jobs) == 1:
        for job in jobs:
            try:
                merge(_extract_worker(job))
            except Exception as e:
                print(f"[warn] 読み込み失敗 ({job['name']}): {e}")
    else:
        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_extract_worker, job): job for job in jobs}
                for fut in as_completed(futures):
                    try:
                        merge(fut.result())
                    except Exception as e:
                        print(f"[warn] 読み込み失敗 ({futures[fut]['name']}): {e}")
        except (OSError, RuntimeError) as e:  # プロセス起動に失敗したら直列で実行
            print(f"[warn] 並列実行に失敗したため直列で処理します: {e}")
            for job in jobs:
                try:
                    merge(_extract_worker(job))
                except Exception as e2:
                    print(f"[warn] 読み込み失敗 ({job['name']}): {e2}")

    for err, n in decode_errors.items():
        print(f"[warn] デコード失敗 {n} 件: {err}")
    return per_topic


def topic_columns(fields, rows):
    """トピックの出力列名を決める。fields が空ならデータ中の全キーを列にする。"""
    if fields:
        return [f.split(".")[-1] for f in fields]
    cols = OrderedDict()
    for r in rows:
        for k in r:
            if k != "t_ns":
                cols[k] = True
    return list(cols.keys())


def window_base(prefix, ws_ns, we_ns):
    """分割出力 1 区間分の出力ファイル名ベースを作る (日をまたぐ区間は終了側にも日付)。"""
    ws = datetime.datetime.fromtimestamp(ws_ns / 1e9, JST)
    we = datetime.datetime.fromtimestamp(we_ns / 1e9, JST)
    end_fmt = "%Y%m%d_%H%M%S" if we.date() != ws.date() else "%H%M%S"
    return f"{prefix}_{ws:%Y%m%d_%H%M%S}-{we.strftime(end_fmt)}"


def write_csvs_split(per_topic, topic_config, outdir, base_prefix, start_ns, end_ns,
                     split_minutes, merged=True, merged_grid=None, merged_hold=5.0):
    """指定期間を split_minutes 分ごとの区間に刻み、区間ごとに CSV 一式を書き出す。

    長時間の抽出 (特に結合 CSV) は 1 ファイルが巨大になるため、抽出済みデータを
    書き出しの段階で分割する (mcap の読み込みは 1 回のまま)。区間ごとに出力ファイル名の
    時間帯が変わり、t_sec も各区間の先頭からの経過秒になる (各ファイルが自己完結)。
    start_ns / end_ns が None の場合はデータの実時刻範囲を使う。
    戻り値: 書き出した全ファイルパスのリスト。
    """
    all_t = [r["t_ns"] for rows in per_topic.values() for r in rows]
    if not all_t:
        print("[warn] 対象トピックのメッセージが 1 件も見つかりませんでした。")
        return []
    s_ns = start_ns if start_ns is not None else min(all_t)
    e_ns = end_ns if end_ns is not None else max(all_t) + 1
    step_ns = max(int(split_minutes * 60 * 1e9), 1)
    written = []
    n_win = 0
    ws = s_ns
    while ws < e_ns:
        we = min(ws + step_ns, e_ns)
        sub = {t: [r for r in rows if ws <= r["t_ns"] < we]
               for t, rows in per_topic.items()}
        if any(sub.values()):
            n_win += 1
            written += write_csvs(sub, topic_config, outdir,
                                  window_base(base_prefix, ws, we), merged=merged,
                                  merged_grid=merged_grid, merged_hold=merged_hold)
        ws = we
    print(f"[info] 分割出力: {split_minutes:g} 分 × {n_win} 区間 "
          f"(データの無い区間はスキップ)")
    return written


def _grid_label(grid_sec):
    """グリッド周期のファイル名用ラベル (0.1 → '100ms', 1.0 → '1s')。"""
    ms = round(grid_sec * 1000)
    if ms < 1000:
        return f"{ms}ms"
    return f"{grid_sec:g}s"


def write_merged_grid_csv(per_topic, topic_config, outdir, base, grid_sec, hold_sec):
    """全トピックを一定周期の共通時間軸に揃えた結合 CSV を 1 本書き出す。

    周波数が異なるトピックをそのまま時刻順に並べると他トピックの列が歯抜けになり
    解析・図示に向かないため、grid_sec 間隔の時刻グリッドを作り、各セルには
    「その時刻までに観測された最新値」を入れる (前値ホールド / zero-order hold。
    計測ツールの一般的な揃え方)。hold_sec (秒) を超えて更新が無い区間は空欄にして、
    実際のデータ欠損が古い値で埋まって見えないようにする (hold_sec<=0 で無制限)。
    列名は「<トピック suffix>.<列名>」とし、トピック間の同名列の衝突を避ける。
    戻り値: 書き出したファイルパスのリスト。
    """
    all_t = [r["t_ns"] for rows in per_topic.values() for r in rows]
    if not all_t:
        return []
    grid_ns = max(int(round(grid_sec * 1e9)), 1)
    hold_ns = None if not hold_sec or hold_sec <= 0 else int(round(hold_sec * 1e9))
    t_start = (min(all_t) // grid_ns) * grid_ns  # グリッドを周期の倍数に吸着
    t_end = max(all_t)
    n_grid = int((t_end - t_start) // grid_ns) + 1

    # 列定義と、トピックごとの時刻順データを用意
    col_defs = []   # (topic, 列キー, ヘッダ名)
    rows_by_topic = {}
    for topic in topic_config.keys():
        rows = sorted(per_topic.get(topic, []), key=lambda r: r["t_ns"])
        if not rows:
            continue
        rows_by_topic[topic] = rows
        suffix = topic_config[topic]["suffix"]
        rename = topic_config[topic].get("rename") or {}
        for c in topic_columns(topic_config[topic]["fields"], rows):
            # 出力名の指定があればそのまま使う (客先向け表示名など)。無ければ
            # 「suffix.列名」でトピック間の同名列の衝突を避ける
            col_defs.append((topic, c, rename.get(c) or f"{suffix}.{c}"))
    if not col_defs:
        return []
    dup = {h for h in (x[2] for x in col_defs)
           if sum(1 for x in col_defs if x[2] == h) > 1}
    if dup:
        print(f"[warn] 結合 CSV の列名が重複しています (rename の指定を見直してください): "
              + ", ".join(sorted(dup)))

    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, f"{base}_all_{_grid_label(grid_sec)}.csv")
    cursor = {t: 0 for t in rows_by_topic}   # 次に消費する行番号
    latest = {t: None for t in rows_by_topic}  # グリッド時刻までの最新行
    with open(out, "w", newline="", encoding="utf-8-sig") as g:
        w = csv.writer(g)
        w.writerow(["time_jst", "t_sec", "t_ns"] + [h for _, _, h in col_defs])
        for k in range(n_grid):
            gt = t_start + k * grid_ns
            for topic, rows in rows_by_topic.items():
                i = cursor[topic]
                while i < len(rows) and rows[i]["t_ns"] <= gt:
                    latest[topic] = rows[i]
                    i += 1
                cursor[topic] = i
            vals = []
            for topic, c, _h in col_defs:
                cur = latest[topic]
                if cur is None or (hold_ns is not None and gt - cur["t_ns"] > hold_ns):
                    vals.append("")
                else:
                    vals.append(cur.get(c, ""))
            w.writerow([fmt_jst(gt), round((gt - t_start) / 1e9, 3), gt] + vals)
    hold_str = "無制限" if hold_ns is None else f"{hold_sec:g}s"
    print(f"[ok] wrote {out}  ({n_grid} rows, 周期 {_grid_label(grid_sec)}, "
          f"前値ホールド上限 {hold_str})")
    return [out]


def write_csvs(per_topic, topic_config, outdir, base, merged=True,
               merged_grid=None, merged_hold=5.0):
    """トピック別 CSV と (トピック指定時のみ) 全トピック結合 CSV を書き出す。

    topic_config が None のときは全トピックモード。トピックごとに CSV を 1 本ずつ
    出力し、列数が膨大になる結合 CSV は作らない。merged=False でも結合 CSV を省く。
    merged_grid (秒) を指定すると、結合 CSV は時刻順のメッセージ行の代わりに
    共通時間軸へ揃えた形式 (write_merged_grid_csv) で出力する。
    """
    all_topics = topic_config is None
    all_t = [r["t_ns"] for rows in per_topic.values() for r in rows]
    if not all_t:
        print("[warn] 対象トピックのメッセージが 1 件も見つかりませんでした。")
        print("       --list-topics で実際のトピック名を確認し、--topics の設定を見直してください。")
        return []
    t0 = min(all_t)
    os.makedirs(outdir, exist_ok=True)
    written = []

    def time_cols(t_ns):
        return [fmt_jst(t_ns), round((t_ns - t0) / 1e9, 3), t_ns]

    def suffix_of(topic):
        if not all_topics and topic in topic_config:
            return topic_config[topic]["suffix"]
        return topic.strip("/").replace("/", "_")

    def fields_of(topic):
        if all_topics:
            return []
        return topic_config[topic]["fields"]

    # 出力順: トピック指定時は設定順、全トピック時は名前順
    topics = sorted(per_topic.keys()) if all_topics else list(topic_config.keys())

    # (1) トピック別 CSV
    merged_cols = OrderedDict()
    for topic in topics:
        rows = sorted(per_topic.get(topic, []), key=lambda r: r["t_ns"])
        if not rows:
            print(f"[info] メッセージなし: {topic}")
            continue
        cols = topic_columns(fields_of(topic), rows)
        for c in cols:
            merged_cols[c] = True
        rename = {} if all_topics else (topic_config[topic].get("rename") or {})
        out = os.path.join(outdir, f"{base}_{suffix_of(topic)}.csv")
        with open(out, "w", newline="", encoding="utf-8-sig") as g:
            w = csv.writer(g)
            w.writerow(["time_jst", "t_sec", "t_ns"] + [rename.get(c, c) for c in cols])
            for r in rows:
                w.writerow(time_cols(r["t_ns"]) + [r.get(c, "") for c in cols])
        print(f"[ok] wrote {out}  ({len(rows)} rows)")
        written.append(out)

    # 全トピックモードは結合 CSV を作らない (列数が膨大になり実用的でないため)
    if all_topics or not merged:
        if all_topics:
            print(f"[info] 全トピックモードのため結合 CSV (_all.csv) は作成しません "
                  f"(トピック別 CSV を {len(written)} 本出力)。")
        return written

    # (2a) 共通時間軸へ揃えた結合 CSV (周期指定時)
    if merged_grid:
        written += write_merged_grid_csv(per_topic, topic_config, outdir, base,
                                         merged_grid, merged_hold)
        return written

    # (2) 全トピック結合 CSV (時刻順 / 値が無い列は空欄)
    all_cols = list(merged_cols.keys())
    suffix_by_topic = {t: suffix_of(t) for t in topics}
    all_rows = sorted(
        [(topic, r) for topic, rows in per_topic.items() for r in rows],
        key=lambda x: x[1]["t_ns"],
    )
    out_all = os.path.join(outdir, f"{base}_all.csv")
    with open(out_all, "w", newline="", encoding="utf-8-sig") as g:
        w = csv.writer(g)
        w.writerow(["time_jst", "t_sec", "t_ns", "topic"] + all_cols)
        for topic, r in all_rows:
            w.writerow(time_cols(r["t_ns"]) + [suffix_by_topic[topic]]
                       + [r.get(c, "") for c in all_cols])
    print(f"[ok] wrote {out_all}  ({len(all_rows)} rows)")
    written.append(out_all)
    return written


# ------------------------------------------------------------------
# GCS 検索の共通入口 (CLI / UI 共用)
# ------------------------------------------------------------------
def find_gcs_sources(client, bucket, vehicle, start_dt, end_dt, subdir=DEFAULT_SUBDIR,
                     lookback_hours=24, workers=16, include_sensor=False,
                     sensor_subdir="record_sensor", include_image=False,
                     image_subdir="record_debug_image"):
    """条件に合う mcap ソースを GCS から探して返す。見つからなければ LookupError。"""
    vehicle = vehicle.strip().upper()
    start_ns, end_ns = to_ns(start_dt), to_ns(end_dt)
    print(f"[info] バケット {bucket} から {vehicle} / "
          f"{start_dt:%Y-%m-%d %H:%M:%S} - {end_dt:%Y-%m-%d %H:%M:%S} (JST) を探索")
    top_dirs = list_top_dirs(client, bucket, vehicle, start_dt, end_dt)
    if not top_dirs:
        raise LookupError(f"該当ディレクトリがありません (パターン: YYYYMMDD_{vehicle}__*)。"
                          "日付と vehicle ID を確認してください。")
    for d in top_dirs:
        print(f"[info] 候補ディレクトリ: {d}")
    blobs = list_candidate_blobs(client, bucket, top_dirs, subdir,
                                 start_dt, end_dt, lookback_hours)
    if not blobs:
        raise LookupError(f"mcap ファイルが見つかりません (サブディレクトリ: {subdir})。"
                          "--subdir \"*\" や --session-lookback の拡大も試してください。")
    print(f"[info] mcap 候補 {len(blobs)} 件。時刻メタデータで絞り込み中"
          f" (並列 {workers}, セッション単位の二分探索)...")
    sources = filter_sources_by_time([GcsMcapSource(b) for b in blobs],
                                     start_ns, end_ns, workers=workers)
    base_sources = list(sources)  # 兄弟探索は develop 側の選定結果を基準にする
    for flag, sib_subdir in ((include_image, image_subdir),
                             (include_sensor, sensor_subdir)):
        if flag and base_sources:
            sources += find_sibling_files(client, bucket, base_sources, sib_subdir)
    return sources


# ------------------------------------------------------------------
# mcap 出力 (時間帯クロップ / トピック絞り込み / 元ファイル保存)
# ------------------------------------------------------------------
def save_mcap_slice(sources, topics, start_ns, end_ns, out_path, progress=None):
    """複数ソースから指定時間帯・指定トピックのメッセージを 1 本の mcap に書き出す。

    メッセージはデコードせず生データのままコピーする (スキーマ・チャンネルも引き継ぐ)。
    topics が None のときは全トピック。
    progress: callable(完了ファイル数, 総ファイル数, 直近ファイル名)。
    """
    from mcap.writer import Writer  # Writer

    n_msgs = 0
    ordered = sorted(sources, key=lambda s: s.name)
    with open(out_path, "wb") as fo:
        w = Writer(fo)
        w.start()
        schema_ids = {}   # (name, encoding, data) -> 新しい schema id
        channel_ids = {}  # (topic, message_encoding, schema id) -> 新しい channel id
        for i, src in enumerate(ordered):
            print(f"[info] mcap 読み込み中: {src.name}")
            try:
                with src.open() as f:
                    reader = make_reader(f)
                    for schema, channel, message in reader.iter_messages(
                            topics=topics, start_time=start_ns, end_time=end_ns,
                            log_time_order=True):
                        if schema is not None:
                            skey = (schema.name, schema.encoding, schema.data)
                            if skey not in schema_ids:
                                schema_ids[skey] = w.register_schema(
                                    schema.name, schema.encoding, schema.data)
                            sid = schema_ids[skey]
                        else:
                            sid = 0
                        ckey = (channel.topic, channel.message_encoding, sid)
                        if ckey not in channel_ids:
                            channel_ids[ckey] = w.register_channel(
                                channel.topic, channel.message_encoding, sid,
                                dict(channel.metadata))
                        w.add_message(channel_ids[ckey], message.log_time,
                                      message.data, message.publish_time,
                                      message.sequence)
                        n_msgs += 1
            except Exception as e:
                print(f"[warn] 読み込み失敗 ({src.name}): {e}")
            if progress:
                progress(i + 1, len(ordered), src.name)
        w.finish()
    print(f"[ok] wrote {out_path}  ({n_msgs} messages, {size_str(os.path.getsize(out_path))})")
    return out_path, n_msgs


def download_raw_mcaps(sources, outdir, workers=4, progress=None, cache_dir=None):
    """対象の mcap を元ファイルのまま outdir に保存する (GCS は並列ダウンロード)。

    progress: callable(done_bytes, total_bytes, done_files, total_files)。
    ダウンロード中 0.5 秒おきに呼ばれる (UI の進捗バー更新用)。
    cache_dir を指定するとキャッシュ済みファイルは GCS を読まずにコピーし、
    新規ダウンロードはキャッシュにも保存する。
    """
    os.makedirs(outdir, exist_ok=True)
    written = []
    total_bytes = sum(s.size or 0 for s in sources)
    state = {"bytes": 0, "files": 0}
    lock = threading.Lock()

    def _download_counted(src, dest_path):
        """チャンク単位で読みながら書く (進捗と GCS 転送量を数える)。"""
        raw = src.blob.open("rb", chunk_size=32 * 1024 * 1024)
        with raw as fin, open(dest_path, "wb") as fout:
            while True:
                chunk = fin.read(8 * 1024 * 1024)
                if not chunk:
                    break
                fout.write(chunk)
                STATS.add_gcs(len(chunk))
                with lock:
                    state["bytes"] += len(chunk)

    def fetch(src):
        dest = os.path.join(outdir, src.name.rsplit("/", 1)[-1])
        if isinstance(src, GcsMcapSource):
            cached = cache_lookup(cache_dir, src.blob.bucket.name, src.blob.name, src.size)
            if cached:
                shutil.copyfile(cached, dest)
                STATS.add_cache(os.path.getsize(dest))
                with lock:
                    state["bytes"] += os.path.getsize(dest)
            elif cache_dir:
                cpath = cache_file_path(cache_dir, src.blob.bucket.name, src.blob.name)
                os.makedirs(os.path.dirname(cpath), exist_ok=True)
                _download_counted(src, cpath + ".part")
                os.replace(cpath + ".part", cpath)
                shutil.copyfile(cpath, dest)
            else:
                _download_counted(src, dest)
        else:
            shutil.copyfile(src.path, dest)
            with lock:
                state["bytes"] += src.size or 0
        with lock:
            state["files"] += 1
        return dest

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(fetch, src): src for src in sources}
        pending = set(futures)
        while pending:
            done_now, pending = wait(pending, timeout=0.5)
            for fut in done_now:
                try:
                    dest = fut.result()
                    print(f"[ok] saved {dest}  ({size_str(os.path.getsize(dest))})")
                    written.append(dest)
                except Exception as e:
                    print(f"[warn] 保存失敗 ({futures[fut].name}): {e}")
            if progress:
                with lock:
                    b, fdone = state["bytes"], state["files"]
                progress(b, total_bytes, fdone, len(sources))
    if progress:
        progress(total_bytes, total_bytes, len(sources), len(sources))
    return sorted(written)


# ------------------------------------------------------------------
# トピック一覧 (--list-topics)
# ------------------------------------------------------------------
def collect_topics(sources, workers=8):
    """対象ソースのサマリからトピック情報を集める。

    戻り値: {topic: {"schema": 名前, "encoding": エンコーディング, "count": メッセージ数}}
    """
    def one(src):
        info = {}
        try:
            with src.open(chunk_size=256 * 1024) as f:
                summary = make_reader(f).get_summary()
            if summary is None:
                return info
            stats = summary.statistics
            counts = stats.channel_message_counts if stats else {}
            for ch in summary.channels.values():
                sc = summary.schemas.get(ch.schema_id)
                info[ch.topic] = {
                    "schema": sc.name if sc else "?",
                    "encoding": sc.encoding if sc else "?",
                    "count": counts.get(ch.id, 0),
                }
        except Exception as e:
            print(f"[warn] トピック取得失敗 ({src.name}): {e}")
        return info

    merged = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for info in pool.map(one, sources):
            for topic, meta in info.items():
                if topic in merged:
                    merged[topic]["count"] += meta["count"]
                else:
                    merged[topic] = dict(meta)
    return dict(sorted(merged.items()))
def print_topics(sources):
    for src in sources:
        print(f"\n=== {src.name} ===")
        try:
            with src.open(chunk_size=1024 * 1024) as f:
                reader = make_reader(f)
                summary = reader.get_summary()
                if summary is None:
                    print("  (サマリなし)")
                    continue
                stats = summary.statistics
                counts = stats.channel_message_counts if stats else {}
                schemas = summary.schemas
                rows = []
                for ch in summary.channels.values():
                    sc = schemas.get(ch.schema_id)
                    rows.append((ch.topic,
                                 sc.name if sc else "?",
                                 sc.encoding if sc else "?",
                                 counts.get(ch.id, 0)))
                rows.sort()
                width = max((len(r[0]) for r in rows), default=10)
                for topic, name, enc, n in rows:
                    print(f"  {topic:<{width}}  {n:>8} msgs  [{enc}] {name}")
        except Exception as e:
            print(f"  [warn] 読み込み失敗: {e}")


# ------------------------------------------------------------------
# 転送量の見積もり (トピック絞り込みでどれだけダウンロードを減らせるか)
# ------------------------------------------------------------------
def _parse_message_indexes(buf):
    """チャンク末尾の MessageIndex レコード群から {channel_id: [チャンク内オフセット...]} を得る。"""
    out = defaultdict(list)
    i = 0
    n = len(buf)
    while i + 9 <= n:
        op = buf[i]
        ln = int.from_bytes(buf[i + 1:i + 9], "little")
        body = buf[i + 9:i + 9 + ln]
        if op == 0x07 and len(body) >= 6:  # MessageIndex
            ch = int.from_bytes(body[0:2], "little")
            arr_len = int.from_bytes(body[2:6], "little")
            j, end = 6, min(6 + arr_len, len(body))
            while j + 16 <= end:
                out[ch].append(int.from_bytes(body[j + 8:j + 16], "little"))
                j += 16
        i += 9 + ln
    return out

def _sample_selected_uncomp(f, chunk_index, sel_ids):
    """1 チャンクのメッセージインデックスだけ読み、(選択トピックの非圧縮バイト, チャンク非圧縮バイト)。

    チャンク本体は読まず、末尾のメッセージインデックス (数十KB) だけをシーク読みする。
    """
    ci = chunk_index
    if not ci.message_index_length:
        return 0, ci.uncompressed_size
    f.seek(ci.chunk_start_offset + ci.chunk_length)
    buf = f.read(ci.message_index_length)
    offsets = _parse_message_indexes(buf)
    all_offs = sorted(o for offs in offsets.values() for o in offs)
    bounds = all_offs + [ci.uncompressed_size]
    span = {o: bounds[k + 1] - o for k, o in enumerate(all_offs)}
    sel = sum(span.get(o, 0) for cid in sel_ids for o in offsets.get(cid, []))
    return sel, ci.uncompressed_size


def analyze_transfer(source, topics, start_ns=None, end_ns=None, cache_dir=None,
                     sample_chunks=3):
    """1 ファイルについて、選択トピック抽出に必要な転送量をサマリから見積もる。

    合計値 (total/needed/win_uncomp/圧縮方式) はサマリのみから即座に求める
    (追加ダウンロードなし)。実データ比率のための非圧縮バイトは、先頭付近の
    最大 sample_chunks 個のチャンクのメッセージインデックスだけを読み、
    その比率を必要チャンク全体へ外挿する (0 なら比率は測らない)。

    戻り値 dict:
      total_bytes      : 時間帯内チャンクの合計 (一括ダウンロード相当)
      needed_bytes     : 選択トピックを含むチャンクだけの合計 (チャンクスキップ後)
      sel_uncomp_bytes : 選択トピックのメッセージ実バイト (非圧縮換算, 外挿。不明なら None)
      win_uncomp_bytes : 時間帯内チャンクの非圧縮合計
      compressions     : チャンク圧縮方式の集合 (例 {"zstd"} / {"none"})
      cached           : キャッシュ済みか (True なら転送ゼロで読める)
      ratio_sampled    : sel_uncomp が全数計測でなく外挿か
    """
    cached = None
    if isinstance(source, GcsMcapSource) and cache_dir:
        cached = cache_lookup(cache_dir, source.blob.bucket.name,
                              source.blob.name, source.size)
    f = open(cached, "rb") if cached else source.open(chunk_size=256 * 1024)
    with f:
        summary = make_reader(f).get_summary()
        if summary is None or not summary.chunk_indexes:
            return None
        sel_ids = {cid for cid, ch in summary.channels.items()
                   if topics is None or ch.topic in set(topics)}
        total = needed = win_uncomp = needed_uncomp = 0
        comps = set()
        needed_chunks = []
        for ci in summary.chunk_indexes:
            if start_ns is not None and ci.message_end_time < start_ns:
                continue
            if end_ns is not None and ci.message_start_time >= end_ns:
                continue
            total += ci.chunk_length + ci.message_index_length
            win_uncomp += ci.uncompressed_size
            comps.add(ci.compression or "none")
            if any(cid in ci.message_index_offsets for cid in sel_ids):
                # ストリーミングはチャンク本体だけ読む (チャンク間のメッセージ
                # インデックスはシークで飛ばす) ため、必要量はチャンク長のみ
                needed += ci.chunk_length
                needed_uncomp += ci.uncompressed_size
                needed_chunks.append(ci)

        # 実データ比率は先頭の数チャンクだけサンプリングして外挿する (I/O を限定)
        sel_uncomp = None
        ratio_sampled = False
        if sample_chunks and needed_chunks:
            local = bool(cached) or isinstance(source, LocalMcapSource)
            n_sample = len(needed_chunks) if local else min(sample_chunks, len(needed_chunks))
            sampled_sel = sampled_uncomp = 0
            ok = False
            for ci in needed_chunks[:n_sample]:
                try:
                    s, u = _sample_selected_uncomp(f, ci, sel_ids)
                    sampled_sel += s
                    sampled_uncomp += u
                    ok = True
                except Exception:
                    continue
            if ok and sampled_uncomp > 0:
                frac = sampled_sel / sampled_uncomp
                sel_uncomp = frac * needed_uncomp
                ratio_sampled = n_sample < len(needed_chunks)
        return {
            "total_bytes": total,
            "needed_bytes": needed,
            "sel_uncomp_bytes": sel_uncomp,
            "win_uncomp_bytes": win_uncomp,
            "compressions": comps,
            "cached": bool(cached),
            "ratio_sampled": ratio_sampled,
        }


def estimate_transfer_report(sources, topics, start_ns=None, end_ns=None,
                             cache_dir=None, workers=8):
    """複数ファイルの転送量見積もりをまとめる。戻り値: (per-file リスト, 集計 dict)"""
    def one(src):
        try:
            r = analyze_transfer(src, topics, start_ns, end_ns, cache_dir)
        except Exception as e:
            print(f"[warn] 見積もり失敗 ({src.name}): {e}")
            return None
        if r is not None:
            r["name"] = src.name
        return r

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        rows = [r for r in pool.map(one, sources) if r]

    have_ratio = [r for r in rows if r["sel_uncomp_bytes"] is not None]
    agg = {
        "total": sum(r["total_bytes"] for r in rows),
        "needed": sum(r["needed_bytes"] for r in rows),
        "sel_uncomp": sum(r["sel_uncomp_bytes"] for r in have_ratio) if have_ratio else None,
        "win_uncomp": sum(r["win_uncomp_bytes"] for r in have_ratio),
        "cached": sum(r["total_bytes"] for r in rows if r["cached"]),
        "compressions": set().union(*(r["compressions"] for r in rows)) if rows else set(),
        "ratio_sampled": any(r.get("ratio_sampled") for r in rows),
    }
    return rows, agg


def print_transfer_estimate(rows, agg):
    """見積もり結果を CLI 向けに表示する。"""
    if not rows:
        print("[warn] 見積もりできるファイルがありません。")
        return
    total, needed = agg["total"], agg["needed"]
    print("\n=== 転送量の見積もり (選択トピックでの抽出) ===")
    for r in rows:
        pct = 100.0 * (1 - r["needed_bytes"] / r["total_bytes"]) if r["total_bytes"] else 0.0
        mark = " [キャッシュ済→転送ゼロ]" if r["cached"] else ""
        print(f"  {r['name'].rsplit('/', 1)[-1]:<40} 全体 {size_str(r['total_bytes']):>9} "
              f"→ 必要 {size_str(r['needed_bytes']):>9} (削減 {pct:4.1f}%){mark}")
    pct = 100.0 * (1 - needed / total) if total else 0.0
    print(f"  合計: 全体 {size_str(total)} → チャンクスキップ後 {size_str(needed)} "
          f"(削減 {pct:.1f}%, {cost_str(total)} → {cost_str(needed)})")
    if agg["sel_uncomp"] is not None and agg["win_uncomp"]:
        approx = "≈" if agg["ratio_sampled"] else ""
        share = 100.0 * agg["sel_uncomp"] / agg["win_uncomp"]
        print(f"  選択トピックの実データ比率: {approx}{share:.1f}% (非圧縮換算)")
        if agg["compressions"] <= {"none", ""}:
            print(f"  → チャンクが非圧縮のため、メッセージ単位の範囲取得を実装すれば "
                  f"理論上 約{100 - share:.0f}% 削減の余地あり")
        else:
            comp = "/".join(sorted(agg["compressions"]))
            print(f"  → チャンクは圧縮済み ({comp}) のため、チャンク単位より細かい取得は不可。"
                  f"これ以上の削減は GCP 内での実行 (egress 無料) を検討")


def sample_topic_columns(sources, topics, cache_dir=None, samples_per_topic=3,
                         start_ns=None, end_ns=None):
    """各トピックのメッセージを数件だけデコードし、フラット展開後の列名一覧を返す。

    UI のカラム絞り込み用。チャンクインデックスにより先頭付近のチャンクしか
    読まないため軽い (キャッシュ済みファイルがあれば GCS を読まない)。
    戻り値: {topic: [列名, ...]}
    """
    factories = build_decoder_factories(quiet=True)
    remaining = set(topics)
    counts = defaultdict(int)
    cols = {t: OrderedDict() for t in topics}

    for src in sources:
        if not remaining:
            break
        f = None
        try:
            if isinstance(src, GcsMcapSource) and cache_dir:
                cached = cache_lookup(cache_dir, src.blob.bucket.name,
                                      src.blob.name, src.size)
                if cached:
                    f = open(cached, "rb")
            if f is None:
                f = src.open(chunk_size=4 * 1024 * 1024)
            with f:
                reader = make_reader(f, decoder_factories=factories)
                # iter_decoded_messages だと必要数が揃うまで全メッセージを
                # デコードしてしまい重い (ros2idl の Python デコードは CPU 高負荷)。
                # 生メッセージで受け、各トピック samples_per_topic 件だけデコードする。
                decoders = {}  # channel.id -> デコード関数 (None = デコーダなし)

                def _decoder_for(schema, channel):
                    if channel.id not in decoders:
                        d = None
                        for fac in factories:
                            d = fac.decoder_for(channel.message_encoding, schema)
                            if d is not None:
                                break
                        decoders[channel.id] = d
                    return decoders[channel.id]

                it = reader.iter_messages(
                    topics=sorted(remaining), start_time=start_ns, end_time=end_ns)
                while remaining:
                    try:
                        schema, channel, message = next(it)
                    except StopIteration:
                        break
                    except Exception:
                        continue  # 個別メッセージの読み込み失敗はスキップ
                    topic = channel.topic
                    if topic not in cols or counts[topic] >= samples_per_topic:
                        continue  # 既に揃ったトピックはデコードせず読み飛ばす (安価)
                    try:
                        dec = _decoder_for(schema, channel)
                        if dec is None:
                            remaining.discard(topic)  # デコーダが無ければ待っても無駄
                            continue
                        for k in flatten(dec(message.data)):
                            cols[topic][k] = True
                        counts[topic] += 1
                    except Exception:
                        continue
                    if counts[topic] >= samples_per_topic:
                        remaining.discard(topic)
        except Exception as e:
            print(f"[warn] カラム取得失敗 ({src.name}): {e}")
    for t in topics:
        if not cols[t]:
            print(f"[warn] カラムを取得できませんでした: {t}")
    return {t: list(c) for t, c in cols.items() if c}


def print_transfer_summary():
    """今回の実行で GCS から読んだ量とキャッシュ節約分を表示する。"""
    gcs_b, cache_b = STATS.snapshot()
    if not gcs_b and not cache_b:
        return
    # 同一リージョンの VM 内実行 (run_on_gcp.sh が設定) では GCS 読み込みは egress 無料。
    # egress 課金額を出すと誤解を招くため、無料である旨を表示する。
    if os.environ.get("GETMCAP_INREGION") == "1":
        cost_note = "同一リージョン実行のため egress 無料"
    else:
        cost_note = f"egress {cost_str(gcs_b)}"
    msg = f"[info] GCS 読み込み量: {size_str(gcs_b)} ({cost_note})"
    if cache_b:
        msg += f" / キャッシュ利用: {size_str(cache_b)} (節約 {cost_str(cache_b)})"
    print(msg)


# ------------------------------------------------------------------
# main
# ------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="GCS 上の走行データ mcap から指定 vehicle ID・指定時間帯の CSV を抽出する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--vehicle", help="vehicle ID (例: GIGA07)")
    parser.add_argument("--start", help="抽出開始時刻 JST (例: \"2025-12-04 12:00\")")
    parser.add_argument("--end", help="抽出終了時刻 JST (例: \"2025-12-04 12:10\")")
    parser.add_argument("--topics", metavar="JSON",
                        help="トピック設定 JSON ファイル (topics.example.t2.json 参照)")
    parser.add_argument("--all-topics", action="store_true",
                        help="mcap に含まれる全トピックを抽出する (トピック別 CSV を各 1 本ずつ出力)。"
                             "--topics 不要。フィールド名は全て自動展開される")
    parser.add_argument("--outdir", default="out", help="CSV 出力先ディレクトリ (default: out)")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET,
                        help=f"GCS バケット名 (default: {DEFAULT_BUCKET})")
    parser.add_argument("--subdir", default=DEFAULT_SUBDIR,
                        help=f"recording セッション内の対象サブディレクトリ"
                             f" (default: {DEFAULT_SUBDIR}, \"*\" で全て)")
    parser.add_argument("--workers", type=int, default=16,
                        help="時刻メタデータ読み込みの並列数 (default: 16)")
    parser.add_argument("--extract-workers", type=int, default=None, metavar="N",
                        help="抽出 (ダウンロード + デコード) のファイル並列数"
                             " (default: CPU コア数と対象ファイル数から自動)")
    parser.add_argument("--exclude-topics", nargs="*", default=[], metavar="PATTERN",
                        help="除外するトピック (ワイルドカード可)。例: "
                             "--exclude-topics \"/tf*\" \"/t2/positioning_driver/internal/*\"")
    parser.add_argument("--no-download", action="store_true",
                        help="一括ダウンロードせず常にチャンク単位の部分読み込みを使う")
    parser.add_argument("--gcs-files", metavar="JSON",
                        help="抽出対象の GCS mcap 一覧 JSON (gs://bucket/path の配列)。"
                             "指定するとファイル検索をスキップし、この一覧だけを処理する"
                             " (UI の②選択と完全に一致させる用)")
    parser.add_argument("--no-merged", action="store_true",
                        help="結合 CSV (_all.csv) を出力しない")
    parser.add_argument("--merged-grid", type=float, default=None, metavar="SEC",
                        help="結合 CSV を一定周期 (秒) の共通時間軸に揃えて出力する (例: 0.1)。"
                             "各セルはその時刻までの最新値 (前値ホールド)。"
                             "省略時は従来のメッセージ到着順の結合 CSV")
    parser.add_argument("--merged-hold", type=float, default=5.0, metavar="SEC",
                        help="--merged-grid で前値を保持する最大秒数。"
                             "これを超えて更新が無い区間は空欄になる (0=無制限, 既定 5)")
    parser.add_argument("--split-minutes", type=float, default=None, metavar="MIN",
                        help="指定した分数ごとに出力ファイルを区切る (例: 30)。"
                             "長時間の抽出でも 1 ファイルが巨大にならない。"
                             "mcap の読み込みは 1 回で、書き出しだけを分割する")
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR, metavar="DIR",
                        help="一括ダウンロードのローカルキャッシュ先。同じファイルの"
                             f"再ダウンロード (= 再課金) を防ぐ (default: {DEFAULT_CACHE_DIR})")
    parser.add_argument("--no-cache", action="store_true",
                        help="キャッシュを使わない (毎回 GCS から読む)")
    parser.add_argument("--cache-max-gb", type=float, default=DEFAULT_CACHE_MAX_GB,
                        help="キャッシュの上限サイズ GB。超えたら古いものから削除"
                             f" (default: {DEFAULT_CACHE_MAX_GB:.0f})")
    parser.add_argument("--include-sensor", action="store_true",
                        help="record_develop で確定した連番と同じ record_sensor の mcap も"
                             "抽出対象に加える (時刻の再確認はしない)")
    parser.add_argument("--sensor-subdir", default="record_sensor", metavar="DIR",
                        help="--include-sensor で追加するサブディレクトリ名"
                             " (default: record_sensor)")
    parser.add_argument("--include-image", action="store_true",
                        help="record_develop で確定した連番と同じ record_debug_image の mcap も"
                             "抽出対象に加える (時刻の再確認はしない)")
    parser.add_argument("--image-subdir", default="record_debug_image", metavar="DIR",
                        help="--include-image で追加するサブディレクトリ名"
                             " (default: record_debug_image)")
    parser.add_argument("--session-lookback", type=int, default=24, metavar="HOURS",
                        help="抽出開始時刻の何時間前までに始まったセッションを対象にするか"
                             " (default: 24)")
    parser.add_argument("--estimate", action="store_true",
                        help="抽出せず、選択トピックでの転送量 (課金) 見積もりだけ表示する")
    parser.add_argument("--list-only", action="store_true",
                        help="対象 mcap ファイルの一覧表示のみ (ダウンロード・デコードしない)")
    parser.add_argument("--list-topics", action="store_true",
                        help="対象 mcap に含まれるトピック一覧を表示 (フィールド名調査用)")
    parser.add_argument("--local", nargs="*", metavar="PATTERN",
                        help="GCS の代わりにローカルの mcap を処理 (glob パターン可)")
    args = parser.parse_args()

    # --- 入力ソースの決定 ---
    if args.local is not None:
        if args.include_sensor or args.include_image:
            print("[info] --include-sensor / --include-image は GCS モード専用のため無視します"
                  " (--local では対象ファイルを直接パターンで指定してください)")
        patterns = args.local or ["*.mcap"]
        paths = []
        for pat in patterns:
            paths.extend(sorted(glob.glob(pat)))
        if not paths:
            raise SystemExit(f"[error] mcap が見つかりません: {patterns}")
        sources = [LocalMcapSource(p) for p in paths]
        start_ns = to_ns(parse_jst(args.start)) if args.start else None
        end_ns = to_ns(parse_jst(args.end)) if args.end else None
        base = os.path.splitext(os.path.basename(paths[0]))[0]
        if start_ns is not None and end_ns is not None:
            sources = filter_sources_by_time(sources, start_ns, end_ns)
    else:
        if not (args.vehicle and args.start and args.end):
            parser.error("GCS モードでは --vehicle, --start, --end が必須です"
                         " (ローカル処理は --local を使用)")
        vehicle = args.vehicle.strip().upper()
        start_dt, end_dt = parse_jst(args.start), parse_jst(args.end)
        if start_dt >= end_dt:
            raise SystemExit("[error] --start は --end より前にしてください")
        start_ns, end_ns = to_ns(start_dt), to_ns(end_dt)
        base = f"{vehicle}_{start_dt:%Y%m%d_%H%M%S}-{end_dt:%H%M%S}"

        client = gcs_client()
        if args.gcs_files:
            # UI の②で選択されたファイル一覧をそのまま使う (検索・兄弟探索なし)
            with open(args.gcs_files, encoding="utf-8") as f:
                uris = json.load(f)
            sources = []
            buckets = {}
            for uri in uris:
                path = uri[len("gs://"):] if uri.startswith("gs://") else f"{args.bucket}/{uri}"
                bname, blob_name = path.split("/", 1)
                bkt = buckets.setdefault(bname, client.bucket(bname))
                blob = bkt.get_blob(blob_name)  # サイズ等のメタデータを取得
                if blob is None:
                    print(f"[warn] 見つかりません (スキップ): {uri}")
                    continue
                sources.append(GcsMcapSource(blob))
            print(f"[info] --gcs-files 指定: {len(sources)} 件 (ファイル検索をスキップ)")
        else:
            try:
                sources = find_gcs_sources(
                    client, args.bucket, vehicle, start_dt, end_dt,
                    subdir=args.subdir, lookback_hours=args.session_lookback,
                    workers=args.workers, include_sensor=args.include_sensor,
                    sensor_subdir=args.sensor_subdir,
                    include_image=args.include_image,
                    image_subdir=args.image_subdir)
            except LookupError as e:
                raise SystemExit(f"[error] {e}")

    if not sources:
        raise SystemExit("[error] 指定時間帯に重なる mcap がありません。")

    if args.list_only:
        print(f"\n対象ファイル ({len(sources)} 件):")
        for src in sources:
            print(f"  {src.name}  ({size_str(src.size)})")
        print_transfer_summary()
        return

    if args.list_topics:
        print_topics(sources)
        print_transfer_summary()
        return

    # --- 抽出 ---
    if args.all_topics:
        topic_config = None  # None = mcap に含まれる全トピックを抽出
        if args.topics:
            print("[info] --all-topics 指定のため --topics は無視します。")
    else:
        topic_config = load_topic_config(args.topics)
    factories = build_decoder_factories()  # 利用可能デコーダの表示と事前チェック
    if not factories:
        raise SystemExit("[error] 使えるデコーダがありません。"
                         " `pip install -r requirements.txt` を実行してください。")
    cache_dir = None if args.no_cache else args.cache_dir

    if args.estimate:
        topics = None if topic_config is None else list(topic_config)
        rows, agg = estimate_transfer_report(sources, topics, start_ns, end_ns,
                                             cache_dir=cache_dir)
        print_transfer_estimate(rows, agg)
        print_transfer_summary()
        return

    per_topic = extract_rows(sources, topic_config, start_ns, end_ns,
                             exclude_pats=args.exclude_topics,
                             workers=args.extract_workers,
                             no_download=args.no_download,
                             cache_dir=cache_dir)
    if args.split_minutes:
        # 分割時のファイル名は「<プレフィクス>_<区間開始>-<区間終了>」。GCS モードは
        # 車両IDを、ローカルモードは元ファイル名 (base) をプレフィクスに使う
        prefix = vehicle if args.local is None else base
        write_csvs_split(per_topic, topic_config, args.outdir, prefix,
                         start_ns, end_ns, args.split_minutes,
                         merged=not args.no_merged,
                         merged_grid=args.merged_grid, merged_hold=args.merged_hold)
    else:
        write_csvs(per_topic, topic_config, args.outdir, base, merged=not args.no_merged,
                   merged_grid=args.merged_grid, merged_hold=args.merged_hold)
    if cache_dir:
        prune_cache(cache_dir, args.cache_max_gb)
    print_transfer_summary()


if __name__ == "__main__":
    main()
