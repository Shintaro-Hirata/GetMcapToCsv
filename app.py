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

PRESET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcap_presets.json")

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
# ① 検索条件
# ==================================================================
st.header("① 検索条件")

input_mode = st.radio(
    "入力元", ["GCS から検索", "ローカルの mcap"],
    horizontal=True, key="input_mode",
    help="ダウンロード済みの mcap から CSV を抽出する場合は「ローカルの mcap」を選択"
         "（GCS 課金なし）。")
is_gcs = input_mode.startswith("GCS")

if is_gcs:
    col1, col2, col3, col4 = st.columns([1, 1, 1, 1])
    with col1:
        vehicle = st.text_input("車両ID", value="GIGA09", help="例: GIGA07, GIGA09")
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
    col2, col3, col4 = st.columns([1, 1, 1])

with col2:
    date = st.date_input("日付 (JST)", value=datetime.date.today() - datetime.timedelta(days=1))
with col3:
    t_start_text = st.text_input("開始時刻 (HH:MM:SS)", value="12:00:00",
                                 help="秒まで指定可。例: 20:40 / 20:40:15 / 204015")
with col4:
    t_end_text = st.text_input("終了時刻 (HH:MM:SS)", value="12:05:00",
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
            "record_debug_image も含める", value=True,
            help="develop と同じ連番の image ファイルも対象に加える。"
                 "CSV 抽出だけが目的なら、オフにすると GCS 転送量 (課金) を大きく減らせます。")
    with icol2:
        include_sensor = st.checkbox("record_sensor も含める", value=False,
                                     help="develop と同じ連番の sensor ファイルも対象に加える (サイズ大)")

with st.expander("詳細オプション"):
    oc1, oc2, oc3 = st.columns(3)
    with oc1:
        bucket = st.text_input("GCS バケット", value=core.DEFAULT_BUCKET)
        subdir = st.text_input("基準サブディレクトリ", value=core.DEFAULT_SUBDIR)
    with oc2:
        image_subdir = st.text_input("image サブディレクトリ名", value="record_debug_image")
        sensor_subdir = st.text_input("sensor サブディレクトリ名", value="record_sensor")
    with oc3:
        lookback = st.number_input("セッション遡り時間 (h)", value=24, min_value=1, max_value=96)
        meta_workers = st.number_input("メタデータ並列数", value=16, min_value=1, max_value=64)
        extract_workers = st.number_input("抽出並列数 (0=自動)", value=0, min_value=0, max_value=16)

    st.markdown("**ローカルキャッシュ**（同じ mcap の再ダウンロード = 再課金を防ぐ）")
    cc1, cc2, cc3 = st.columns([1, 2, 1])
    with cc1:
        cache_enable = st.checkbox("キャッシュを使う", value=True, key="cache_enable")
    with cc2:
        cache_dir_input = st.text_input("キャッシュフォルダ",
                                        value=os.path.abspath(core.DEFAULT_CACHE_DIR),
                                        key="cache_dir")
    with cc3:
        cache_max_gb = st.number_input("上限 (GB)", value=float(core.DEFAULT_CACHE_MAX_GB),
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
end_dt = datetime.datetime.combine(date, t_end, tzinfo=core.JST)
if end_dt <= start_dt:
    end_dt += datetime.timedelta(days=1)  # 終了が開始より前なら深夜跨ぎとみなす
    if time_ok and time_needed:
        st.info(f"終了時刻が開始時刻以前のため翌日扱いにします: 終了 = {end_dt:%m/%d %H:%M:%S}")
if time_ok and time_needed:
    st.caption(f"🕐 抽出時間帯: {start_dt:%Y-%m-%d %H:%M:%S} 〜 {end_dt:%Y-%m-%d %H:%M:%S} (JST)")

_btn_label = "🔍 ① 候補ファイルを検索" if is_gcs else "📂 ① ローカル mcap を読み込み"
if st.button(_btn_label, type="primary", disabled=(time_needed and not time_ok)):
    ss.sources = None
    ss.topics_info = None
    ss.result_files = None
    ss.file_defaults = None  # 新しい検索結果ではチェック状態を全選択に戻す
    ss.search_transfer = None
    ss.topic_columns = {}
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
                    base = (f"{folder}_{start_dt:%Y%m%d_%H%M%S}-{end_dt:%H%M%S}"
                            if use_filter else folder)
                    ss.search_params = {
                        "start_ns": core.to_ns(start_dt) if use_filter else None,
                        "end_ns": core.to_ns(end_dt) if use_filter else None,
                        "base": base,
                        "extract_workers": int(extract_workers) or None,
                    }

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
        if st.button("📋 トピック一覧を取得"):
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
        with tcol2:
            preset_name = st.selectbox(
                "プリセット (選ぶと下の表の選択に反映)",
                ["(手動選択)"] + list(presets.keys()))
        with tcol3:
            st.write("")
            use_all = st.checkbox("全トピック選択", value=False)

        if use_all:
            default_set = set(all_topic_names)
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
            "メッセージ数": ss.topics_info[t]["count"],
            "エンコード": ss.topics_info[t]["encoding"],
            "スキーマ": ss.topics_info[t]["schema"],
        } for t in all_topic_names]
        edited_topics = st.data_editor(
            pd.DataFrame(topic_rows),
            column_config={
                "選択": st.column_config.CheckboxColumn("選択", width="small"),
                "トピック": st.column_config.TextColumn("トピック", width="large"),
            },
            disabled=["トピック", "メッセージ数", "エンコード", "スキーマ"],
            hide_index=True,
            use_container_width=True,
            height=420,
            key=f"topic_editor_{ss.topics_id}_{preset_name}_{use_all}",
        )
        selected_topics = list(edited_topics[edited_topics["選択"]]["トピック"])
        st.caption(f"✅ 選択中: {len(selected_topics)} / {len(all_topic_names)} トピック")
        if selected_topics:
            with st.expander("選択中のトピックを確認"):
                for t in selected_topics:
                    st.text(t)

        # --- カラム絞り込み (CSV 出力の列を選ぶ。既定は全カラム) ---
        if selected_topics:
            with st.expander("🧩 カラム絞り込み（任意・CSV のみ）"):
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
                if not any(t in ss.topic_columns for t in selected_topics):
                    st.caption("「カラム一覧を取得」を押すと、ここで列を選べます。")
    else:
        st.caption("「トピック一覧を取得」を押すと、ここで選択できます。"
                   " (mcap を元ファイルのまま保存する場合はトピック選択は不要)")

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

    if st.button("🚀 ④ 抽出実行", type="primary", disabled=not can_run):
        params = ss.search_params
        os.makedirs(outdir, exist_ok=True)
        prog = st.progress(0.0, text="準備中...")
        core.STATS.reset()
        try:
            if out_format.startswith("CSV"):
                topic_config = {}
                for t in selected_topics:
                    cfg = {"suffix": t.strip("/").replace("/", "_"), "fields": []}
                    opts = ss.topic_columns.get(t)
                    sel = st.session_state.get(f"colsel_{t}")
                    # 一部の列だけ選ばれていれば絞り込む (全選択/空選択は全カラム扱い)
                    if opts and sel and 0 < len(sel) < len(opts):
                        cfg["columns"] = list(sel)
                    topic_config[t] = cfg

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
                    return core.write_csvs(per_topic, topic_config, outdir,
                                           params["base"], merged=merged_csv)
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
