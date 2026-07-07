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
import sys
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

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
def build_decoder_factories():
    """利用可能な mcap デコーダファクトリを集める。"""
    factories = []
    names = []

    try:
        from mcap_ros2idl_support import Ros2DecodeFactory  # ros2idl (/t2 トピック用)
        factories.append(Ros2DecodeFactory())
        names.append("ros2idl (mcap-ros2idl-support)")
    except ImportError:
        print("[warn] mcap-ros2idl-support が見つかりません。"
              " /t2/* トピック (ros2idl) はデコードできません。README.md を参照。")

    try:
        from mcap_protobuf.decoder import DecoderFactory as PbDecoderFactory  # protobuf 用
        factories.append(PbDecoderFactory())
        names.append("protobuf (mcap-protobuf-support)")
    except ImportError:
        print("[warn] mcap-protobuf-support が見つかりません。"
              " /apollo/* トピック (protobuf) はデコードできません。")

    try:
        from mcap_ros2.decoder import DecoderFactory as Ros2MsgDecoderFactory  # ros2msg 用
        factories.append(Ros2MsgDecoderFactory())
        names.append("ros2msg (mcap-ros2-support)")
    except ImportError:
        pass  # 任意

    if factories:
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
    """時間帯にかかる可能性のあるトップレベルディレクトリ (YYYYMMDD_<vehicle>__*) を列挙。

    深夜跨ぎの走行に備えて開始日の前日から探す。
    """
    dates = []
    d = (start_dt.date() - datetime.timedelta(days=1))
    while d <= end_dt.date():
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


def list_candidate_blobs(client, bucket_name, top_dirs, subdir, start_dt, end_dt,
                         lookback_hours=24):
    """トップレベルディレクトリ配下の mcap blob を列挙する。

    recording/<session>/<subdir>/*.mcap と、直下の <subdir>/*.mcap の両方を探す。
    セッション開始時刻がディレクトリ名 (YYYYMMDD_HHMMSS_...) から分かる場合、
    抽出終了時刻より後に始まるセッションと、抽出開始時刻より lookback_hours 以上
    前に始まったセッションはメタデータを読まずに除外する。
    """
    earliest = start_dt - datetime.timedelta(hours=lookback_hours)
    blobs = []
    skipped_sessions = set()
    for top in top_dirs:
        found_here = []
        for prefix in (f"{top}recording/", f"{top}{subdir}/"):
            for blob in client.list_blobs(bucket_name, prefix=prefix):
                if not blob.name.endswith(".mcap"):
                    continue
                parts = blob.name[len(top):].split("/")
                if parts[0] == "recording":
                    # recording/<session>/<subdir>/<file>.mcap
                    if len(parts) < 4:
                        continue
                    if subdir != "*" and parts[2] != subdir:
                        continue
                    session_start = parse_session_start(parts[1])
                    if session_start is not None and not (earliest <= session_start <= end_dt):
                        skipped_sessions.add(parts[1])
                        continue
                found_here.append(blob)
        blobs.extend(sorted(found_here, key=lambda b: b.name))
    if skipped_sessions:
        print(f"[info] 時間帯外のセッション {len(skipped_sessions)} 件を除外 "
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

    def open(self, chunk_size=16 * 1024 * 1024):
        return self.blob.open("rb", chunk_size=chunk_size)


class LocalMcapSource:
    def __init__(self, path):
        self.path = path
        self.name = path
        self.size = os.path.getsize(path)
        self.session = None  # ローカルは連番の保証がないので個別チェック

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


def size_str(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024.0
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


def extract_rows(sources, topic_config, start_ns, end_ns, factories):
    """全ソースから対象トピックの行データを収集する。

    topic_config が None のときは mcap に含まれる全トピックを対象にする。
    """
    all_topics = topic_config is None
    per_topic = {} if all_topics else {t: [] for t in topic_config}
    decode_errors = defaultdict(int)

    for src in sources:
        print(f"[info] 読み込み中: {src.name}")
        count = 0
        try:
            with src.open() as f:
                reader = make_reader(f, decoder_factories=factories)
                it = reader.iter_decoded_messages(
                    topics=None if all_topics else list(topic_config.keys()),
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
        except Exception as e:
            print(f"[warn] 読み込み失敗 ({src.name}): {e}")
            continue
        print(f"[info]   {count} 行取得")

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


def write_csvs(per_topic, topic_config, outdir, base):
    """トピック別 CSV と (トピック指定時のみ) 全トピック結合 CSV を書き出す。

    topic_config が None のときは全トピックモード。トピックごとに CSV を 1 本ずつ
    出力し、列数が膨大になる結合 CSV は作らない。
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
# トピック一覧 (--list-topics)
# ------------------------------------------------------------------
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
        print(f"[info] バケット {args.bucket} から {vehicle} / "
              f"{start_dt:%Y-%m-%d %H:%M:%S} - {end_dt:%Y-%m-%d %H:%M:%S} (JST) を探索")
        top_dirs = list_top_dirs(client, args.bucket, vehicle, start_dt, end_dt)
        if not top_dirs:
            raise SystemExit(f"[error] 該当ディレクトリがありません "
                             f"(パターン: YYYYMMDD_{vehicle}__*)。日付と vehicle ID を確認してください。")
        for d in top_dirs:
            print(f"[info] 候補ディレクトリ: {d}")
        blobs = list_candidate_blobs(client, args.bucket, top_dirs, args.subdir,
                                     start_dt, end_dt, args.session_lookback)
        if not blobs:
            raise SystemExit("[error] mcap ファイルが見つかりません "
                             f"(サブディレクトリ: {args.subdir})。--subdir \"*\" や"
                             " --session-lookback の拡大も試してください。")
        print(f"[info] mcap 候補 {len(blobs)} 件。時刻メタデータで絞り込み中"
              f" (並列 {args.workers}, セッション単位の二分探索)...")
        sources = filter_sources_by_time([GcsMcapSource(b) for b in blobs],
                                         start_ns, end_ns, workers=args.workers)

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
    factories = build_decoder_factories()
    if not factories:
        raise SystemExit("[error] 使えるデコーダがありません。"
                         " `pip install -r requirements.txt` を実行してください。")
    per_topic = extract_rows(sources, topic_config, start_ns, end_ns, factories)
    write_csvs(per_topic, topic_config, args.outdir, base)


if __name__ == "__main__":
    main()
