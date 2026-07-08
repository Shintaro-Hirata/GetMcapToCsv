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
import sys
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


# ------------------------------------------------------------------
# デコーダ (入っているものを全部使う)
# ------------------------------------------------------------------
def build_decoder_factories(quiet=False):
    """利用可能な mcap デコーダファクトリを集める。"""
    factories = []
    names = []

    try:
        from mcap_ros2idl_support import Ros2DecodeFactory  # ros2idl (/t2 トピック用)
        factories.append(Ros2DecodeFactory())
        names.append("ros2idl (mcap-ros2idl-support)")
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
        return self.blob.open("rb", chunk_size=chunk_size)


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
                if rng is not None:
                    print(f"[info] 対象: {src.name}  "
                          f"({fmt_jst(rng[0])} - {fmt_jst(rng[1])}, {size_str(src.size)})")
                else:
                    print(f"[info] 対象: {src.name}  ({size_str(src.size)})")
            selected.extend(src for src, _ in picked)

    print(f"[info] 絞り込み完了: {len(sources)} 件中 {len(selected)} 件が対象 "
          f"(メタデータ読み込み {total_reads} 回)")
    selected.sort(key=lambda s: s.name)
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
    return config


def _match_any(topic, patterns):
    return any(fnmatch(topic, p) for p in patterns)


def _collect_rows(reader, topic_config, exclude_pats, start_ns, end_ns):
    """開いた mcap reader から行データを収集する。

    topic_config が None のときは全トピック対象。exclude_pats に一致するトピックは
    デコード自体をスキップする (対象トピックのリストを先に確定して reader に渡す)。
    """
    all_topics = topic_config is None
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
            for k, v in flatten(decoded).items():
                r[k] = v
        per_topic[topic].append(r)
        count += 1
    return per_topic, dict(decode_errors), count


def _extract_worker(job):
    """1 ファイル分のダウンロード + デコードを行うワーカー (別プロセスで実行可)。

    戻り値: (表示名, per_topic, decode_errors, 行数, 秒数)
    """
    t0 = time.monotonic()
    factories = build_decoder_factories(quiet=True)
    tmpdir = None
    try:
        if job["kind"] == "gcs":
            from google.cloud import storage  # storage.Client
            blob = storage.Client().bucket(job["bucket"]).blob(job["blob_name"])
            if job["download"]:
                # 時間帯がファイルの大半を占めるなら一括ダウンロードの方が速い
                tmpdir = tempfile.mkdtemp(prefix="getmcap_")
                local = os.path.join(tmpdir, "part.mcap")
                blob.download_to_filename(local)
                f = open(local, "rb")
            else:
                f = blob.open("rb", chunk_size=16 * 1024 * 1024)
        else:
            f = open(job["path"], "rb")
        with f:
            reader = make_reader(f, decoder_factories=factories)
            per_topic, errors, count = _collect_rows(
                reader, job["topic_config"], job["exclude"],
                job["start_ns"], job["end_ns"])
        return job["name"], per_topic, errors, count, time.monotonic() - t0
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
                       no_download=False):
    jobs = []
    for src in sources:
        job = {
            "name": src.name,
            "topic_config": topic_config,
            "exclude": exclude_pats or [],
            "start_ns": start_ns,
            "end_ns": end_ns,
        }
        if isinstance(src, GcsMcapSource):
            job["kind"] = "gcs"
            job["bucket"] = src.blob.bucket.name
            job["blob_name"] = src.blob.name
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
                 workers=None, no_download=False, progress=None):
    """全ソースから対象トピックの行データを収集する (ファイル単位で並列)。

    topic_config が None のときは mcap に含まれる全トピックを対象にする。
    progress: callable(完了ファイル数, 総ファイル数, 直近完了ファイル名)。
    """
    jobs = build_extract_jobs(sources, topic_config, exclude_pats,
                              start_ns, end_ns, no_download)
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
        name, part, errors, count, sec = result
        for topic, rows in part.items():
            per_topic.setdefault(topic, []).extend(rows)
        for err, n in errors.items():
            decode_errors[err] += n
        print(f"[info]   {name.rsplit('/', 1)[-1]}: {count} 行 ({sec:.1f} 秒)")
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


def write_csvs(per_topic, topic_config, outdir, base, merged=True):
    """トピック別 CSV と (トピック指定時のみ) 全トピック結合 CSV を書き出す。

    topic_config が None のときは全トピックモード。トピックごとに CSV を 1 本ずつ
    出力し、列数が膨大になる結合 CSV は作らない。merged=False でも結合 CSV を省く。
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
        out = os.path.join(outdir, f"{base}_{suffix_of(topic)}.csv")
        with open(out, "w", newline="", encoding="utf-8-sig") as g:
            w = csv.writer(g)
            w.writerow(["time_jst", "t_sec", "t_ns"] + cols)
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


def download_raw_mcaps(sources, outdir, workers=4, progress=None):
    """対象の mcap を元ファイルのまま outdir に保存する (GCS は並列ダウンロード)。

    progress: callable(done_bytes, total_bytes, done_files, total_files)。
    ダウンロード中 0.5 秒おきに呼ばれる (UI の進捗バー更新用)。
    """
    os.makedirs(outdir, exist_ok=True)
    written = []
    total_bytes = sum(s.size or 0 for s in sources)
    state = {"bytes": 0, "files": 0}
    lock = threading.Lock()

    def fetch(src):
        dest = os.path.join(outdir, src.name.rsplit("/", 1)[-1])
        if isinstance(src, GcsMcapSource):
            # 進捗を数えるためチャンク単位で読みながら書く
            with src.open(chunk_size=32 * 1024 * 1024) as fin, open(dest, "wb") as fout:
                while True:
                    chunk = fin.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    fout.write(chunk)
                    with lock:
                        state["bytes"] += len(chunk)
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
        return

    if args.list_topics:
        print_topics(sources)
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
    per_topic = extract_rows(sources, topic_config, start_ns, end_ns,
                             exclude_pats=args.exclude_topics,
                             workers=args.extract_workers,
                             no_download=args.no_download)
    write_csvs(per_topic, topic_config, args.outdir, base)


if __name__ == "__main__":
    main()
