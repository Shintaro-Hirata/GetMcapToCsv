#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GetMcapToCsv の Streamlit UI。

起動:
    streamlit run app.py
    (Windows は start_ui.bat をダブルクリックでも可)

流れ:
    1. 車両ID・時間帯を入れて「候補ファイルを検索」
    2. 見つかったファイルを確認 (不要なら外す)
    3. 「トピック一覧を取得」→ 欲しいトピックを選択 (プリセット可)
    4. 出力形式 (CSV / mcap) と出力先を選んで「抽出実行」
"""

import contextlib
import datetime
import io
import json
import os
import subprocess
import sys

import streamlit as st

import get_mcap_to_csv as core  # find_gcs_sources, collect_topics, extract_rows, write_csvs, save_mcap_slice, download_raw_mcaps

PRESET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcap_presets.json")

st.set_page_config(page_title="GetMcapToCsv", page_icon="🚚", layout="wide")
st.title("🚚 GetMcapToCsv — 走行データ mcap 抽出ツール")

ss = st.session_state
ss.setdefault("sources", None)        # 検索で見つかった全ソース
ss.setdefault("search_log", "")
ss.setdefault("topics_info", None)    # {topic: {schema, encoding, count}}
ss.setdefault("topics_log", "")
ss.setdefault("result_files", None)
ss.setdefault("result_log", "")
ss.setdefault("search_params", None)  # 検索時の (start_ns, end_ns, base 名) を保持


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


@st.cache_resource
def gcs_client():
    return core.gcs_client()


# ==================================================================
# ① 検索条件
# ==================================================================
st.header("① 検索条件")

col1, col2, col3, col4 = st.columns([1, 1, 1, 1])
with col1:
    vehicle = st.text_input("車両ID", value="GIGA09", help="例: GIGA07, GIGA09")
with col2:
    date = st.date_input("日付 (JST)", value=datetime.date.today() - datetime.timedelta(days=1))
with col3:
    t_start = st.time_input("開始時刻", value=datetime.time(12, 0), step=60)
with col4:
    t_end = st.time_input("終了時刻", value=datetime.time(12, 5), step=60)

with st.expander("詳細オプション"):
    oc1, oc2, oc3 = st.columns(3)
    with oc1:
        bucket = st.text_input("GCS バケット", value=core.DEFAULT_BUCKET)
        include_sensor = st.checkbox("record_sensor も含める", value=False,
                                     help="develop で確定した連番と同じ sensor ファイルも対象に加える")
    with oc2:
        subdir = st.text_input("サブディレクトリ", value=core.DEFAULT_SUBDIR)
        lookback = st.number_input("セッション遡り時間 (h)", value=24, min_value=1, max_value=96)
    with oc3:
        meta_workers = st.number_input("メタデータ並列数", value=16, min_value=1, max_value=64)
        extract_workers = st.number_input("抽出並列数 (0=自動)", value=0, min_value=0, max_value=16)

start_dt = datetime.datetime.combine(date, t_start, tzinfo=core.JST)
end_dt = datetime.datetime.combine(date, t_end, tzinfo=core.JST)
if end_dt <= start_dt:
    end_dt += datetime.timedelta(days=1)  # 終了が開始より前なら深夜跨ぎとみなす
    st.info(f"終了時刻が開始時刻以前のため翌日扱いにします: 終了 = {end_dt:%m/%d %H:%M}")

if st.button("🔍 ① 候補ファイルを検索", type="primary"):
    ss.sources = None
    ss.topics_info = None
    ss.result_files = None
    try:
        with st.spinner("GCS を検索中... (セッション特定 → 時刻メタデータで絞り込み)"):
            sources, log = run_captured(
                core.find_gcs_sources, gcs_client(), bucket, vehicle,
                start_dt, end_dt, subdir=subdir, lookback_hours=int(lookback),
                workers=int(meta_workers), include_sensor=include_sensor)
        ss.sources = sources
        ss.search_log = log
        ss.search_params = {
            "start_ns": core.to_ns(start_dt),
            "end_ns": core.to_ns(end_dt),
            "base": f"{vehicle.strip().upper()}_{start_dt:%Y%m%d_%H%M%S}-{end_dt:%H%M%S}",
            "extract_workers": int(extract_workers) or None,
        }
    except LookupError as e:
        st.error(f"見つかりませんでした: {e}")
        ss.search_log = ""
    except Exception as e:
        st.error(f"検索に失敗しました: {e}")

if ss.search_log:
    with st.expander("検索ログ"):
        st.code(ss.search_log)

# ==================================================================
# ② 候補ファイルの確認・選択
# ==================================================================
if ss.sources:
    st.header("② 対象ファイル")
    total = sum(s.size or 0 for s in ss.sources)
    st.caption(f"{len(ss.sources)} ファイル / 合計 {core.size_str(total)}")

    labels = {}
    for s in ss.sources:
        rng = getattr(s, "time_range", None)
        t = (f"{core.fmt_jst(rng[0])} - {core.fmt_jst(rng[1])}" if rng else "(時間帯内)")
        labels[s.name] = f"{s.name.rsplit('/', 1)[-1]}  [{t}, {core.size_str(s.size)}]"

    selected_names = st.multiselect(
        "抽出対象 (不要なファイルは × で外す)",
        options=[s.name for s in ss.sources],
        default=[s.name for s in ss.sources],
        format_func=lambda n: labels.get(n, n))
    selected_sources = [s for s in ss.sources if s.name in set(selected_names)]

    # ==============================================================
    # ③ トピック選択
    # ==============================================================
    st.header("③ トピック選択")
    tcol1, tcol2 = st.columns([1, 3])
    with tcol1:
        if st.button("📋 トピック一覧を取得"):
            with st.spinner("トピック一覧を取得中..."):
                info, log = run_captured(core.collect_topics, selected_sources)
            ss.topics_info = info
            ss.topics_log = log
            if not info:
                st.warning("トピックが取得できませんでした。")

    all_topic_names = list(ss.topics_info.keys()) if ss.topics_info else []
    presets = load_presets()

    if ss.topics_info:
        with tcol2:
            pcol1, pcol2 = st.columns([2, 1])
            with pcol1:
                preset_name = st.selectbox(
                    "プリセット (選ぶと下の選択に反映)",
                    ["(手動選択)"] + list(presets.keys()))
            with pcol2:
                st.write("")
                use_all = st.checkbox("全トピック", value=False)

        if use_all:
            default_topics = all_topic_names
        elif preset_name != "(手動選択)":
            wanted = presets[preset_name]
            default_topics = [t for t in all_topic_names
                              if any(core.fnmatch(t, p) for p in wanted)]
            missing = [p for p in wanted
                       if not any(core.fnmatch(t, p) for t in all_topic_names)]
            if missing:
                st.caption(f"⚠ プリセット中でデータに存在しないもの: {', '.join(missing)}")
        else:
            default_topics = []

        def topic_label(t):
            m = ss.topics_info[t]
            return f"{t}  ({m['count']} msgs, {m['encoding']})"

        selected_topics = st.multiselect(
            f"抽出するトピック ({len(all_topic_names)} 個から選択)",
            options=all_topic_names,
            default=default_topics,
            format_func=topic_label)
    else:
        selected_topics = []
        st.caption("「トピック一覧を取得」を押すと、ここで選択できます。")

    # ==============================================================
    # ④ 出力
    # ==============================================================
    st.header("④ 出力")
    ocol1, ocol2 = st.columns([1, 1])
    with ocol1:
        out_format = st.radio("出力形式", [
            "CSV (トピック別)",
            "mcap (時間帯クロップ + トピック絞り込み → 1 ファイル)",
            "mcap (元ファイルをそのまま保存, 1 分刻み)",
        ])
    with ocol2:
        outdir = st.text_input("出力フォルダ", value=os.path.abspath("out"))
        merged_csv = st.checkbox("結合 CSV (_all.csv) も出力", value=False,
                                 help="選択した全トピックを時刻順に 1 本へ結合 (CSV のみ)")

    can_run = bool(selected_sources) and (
        out_format.startswith("mcap (元ファイル") or bool(selected_topics))
    if not can_run and selected_sources:
        st.caption("CSV / mcap(絞り込み) はトピックを 1 つ以上選択してください。")

    if st.button("🚀 ④ 抽出実行", type="primary", disabled=not can_run):
        params = ss.search_params
        os.makedirs(outdir, exist_ok=True)
        try:
            if out_format.startswith("CSV"):
                topic_config = {t: {"suffix": t.strip("/").replace("/", "_"), "fields": []}
                                for t in selected_topics}
                with st.spinner("CSV 抽出中... (ダウンロード + デコード)"):
                    def run():
                        per_topic = core.extract_rows(
                            selected_sources, topic_config,
                            params["start_ns"], params["end_ns"],
                            workers=params["extract_workers"])
                        return core.write_csvs(per_topic, topic_config, outdir,
                                               params["base"], merged=merged_csv)
                    files, log = run_captured(run)
            elif out_format.startswith("mcap (時間帯"):
                out_path = os.path.join(outdir, f"{params['base']}_cropped.mcap")
                with st.spinner("mcap 切り出し中... (生データコピー)"):
                    (out_path, n), log = run_captured(
                        core.save_mcap_slice, selected_sources, selected_topics,
                        params["start_ns"], params["end_ns"], out_path)
                files = [out_path] if n else []
                if n == 0:
                    st.warning("該当メッセージが 0 件でした。トピック選択を確認してください。")
            else:
                with st.spinner("mcap ダウンロード中..."):
                    files, log = run_captured(
                        core.download_raw_mcaps, selected_sources, outdir)
            ss.result_files = files
            ss.result_log = log
        except Exception as e:
            st.error(f"抽出に失敗しました: {e}")

    if ss.result_files is not None:
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
