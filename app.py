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
import io
import json
import os
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

col1, col2, col3, col4 = st.columns([1, 1, 1, 1])
with col1:
    vehicle = st.text_input("車両ID", value="GIGA09", help="例: GIGA07, GIGA09")
with col2:
    date = st.date_input("日付 (JST)", value=datetime.date.today() - datetime.timedelta(days=1))
with col3:
    t_start_text = st.text_input("開始時刻 (HH:MM:SS)", value="12:00:00",
                                 help="秒まで指定可。例: 20:40 / 20:40:15 / 204015")
with col4:
    t_end_text = st.text_input("終了時刻 (HH:MM:SS)", value="12:05:00",
                               help="秒まで指定可。例: 20:45 / 20:45:30 / 204530")

t_start = parse_time_text(t_start_text)
t_end = parse_time_text(t_end_text)
time_ok = t_start is not None and t_end is not None
if t_start is None:
    st.error(f"開始時刻を解釈できません: 「{t_start_text}」 (例: 20:40:15)")
if t_end is None:
    st.error(f"終了時刻を解釈できません: 「{t_end_text}」 (例: 20:45:30)")
if not time_ok:
    t_start = t_start or datetime.time(0, 0)
    t_end = t_end or datetime.time(0, 0)

icol1, icol2, _ = st.columns([1, 1, 2])
with icol1:
    include_image = st.checkbox("record_debug_image も含める", value=True,
                                help="develop と同じ連番の image ファイルも対象に加える (ほぼ毎回使うため既定でオン)")
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

start_dt = datetime.datetime.combine(date, t_start, tzinfo=core.JST)
end_dt = datetime.datetime.combine(date, t_end, tzinfo=core.JST)
if end_dt <= start_dt:
    end_dt += datetime.timedelta(days=1)  # 終了が開始より前なら深夜跨ぎとみなす
    if time_ok:
        st.info(f"終了時刻が開始時刻以前のため翌日扱いにします: 終了 = {end_dt:%m/%d %H:%M:%S}")
if time_ok:
    st.caption(f"🕐 抽出時間帯: {start_dt:%Y-%m-%d %H:%M:%S} 〜 {end_dt:%Y-%m-%d %H:%M:%S} (JST)")

if st.button("🔍 ① 候補ファイルを検索", type="primary", disabled=not time_ok):
    ss.sources = None
    ss.topics_info = None
    ss.result_files = None
    ss.file_defaults = None  # 新しい検索結果ではチェック状態を全選択に戻す
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

    file_rows = []
    for i, s in enumerate(ss.sources):
        rng = getattr(s, "time_range", None)
        file_rows.append({
            "No.": i + 1,
            "選択": ss.file_defaults.get(s.name, True),
            "種類": source_kind(s.name),
            "ファイル名": s.name.rsplit("/", 1)[-1],
            "時間帯 (JST)": (f"{core.fmt_jst(rng[0])} - {core.fmt_jst(rng[1])}"
                          if rng else "(指定時間帯内)"),
            "サイズ": core.size_str(s.size),
            "セッション": s.name.split("/recording/")[-1].split("/")[0]
                        if "/recording/" in s.name else "",
            "パス": s.name,
        })
    edited_files = st.data_editor(
        pd.DataFrame(file_rows),
        column_config={
            "No.": st.column_config.NumberColumn("No.", width="small"),
            "選択": st.column_config.CheckboxColumn("選択", width="small"),
            "パス": None,  # フルパスは非表示 (選択サマリで確認可能)
        },
        disabled=["No.", "種類", "ファイル名", "時間帯 (JST)", "サイズ", "セッション"],
        hide_index=True,
        use_container_width=True,
        key=f"file_editor_{ss.search_id}_{ss.file_ver}",
    )
    current = dict(zip(edited_files["パス"], edited_files["選択"]))

    def apply_file_selection(new_map):
        ss.file_defaults = new_map
        ss.file_ver += 1
        st.rerun()

    # --- 一括操作 ---
    bc1, bc2, bc3, bc4 = st.columns([1, 1, 1, 3])
    with bc1:
        if st.button("☑ 全選択"):
            apply_file_selection({n: True for n in names})
    with bc2:
        if st.button("☐ 全解除"):
            apply_file_selection({n: False for n in names})
    with bc3:
        if st.button("🔁 選択を反転"):
            apply_file_selection({n: not current.get(n, True) for n in names})
    with bc4:
        kinds = sorted({source_kind(n) for n in names})
        kcols = st.columns(max(len(kinds), 1))
        for kc, kind in zip(kcols, kinds):
            with kc:
                if st.button(f"{kind} のみ"):
                    apply_file_selection({n: source_kind(n) == kind for n in names})

    rc1, rc2, rc3, rc4 = st.columns([1, 1, 1, 3])
    with rc1:
        no_from = st.number_input("No. から", min_value=1, max_value=len(names),
                                  value=1, step=1)
    with rc2:
        no_to = st.number_input("No. まで", min_value=1, max_value=len(names),
                                value=len(names), step=1)
    lo, hi = int(min(no_from, no_to)) - 1, int(max(no_from, no_to)) - 1
    with rc3:
        st.write("")
        if st.button("範囲を選択"):
            m = dict(current)
            for n in names[lo:hi + 1]:
                m[n] = True
            apply_file_selection(m)
    with rc4:
        st.write("")
        if st.button("範囲を解除"):
            m = dict(current)
            for n in names[lo:hi + 1]:
                m[n] = False
            apply_file_selection(m)

    selected_sources = [src_by_name[n] for n in names if current.get(n)]
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

    if st.button("🚀 ④ 抽出実行", type="primary", disabled=not can_run):
        params = ss.search_params
        os.makedirs(outdir, exist_ok=True)
        prog = st.progress(0.0, text="準備中...")
        try:
            if out_format.startswith("CSV"):
                topic_config = {t: {"suffix": t.strip("/").replace("/", "_"), "fields": []}
                                for t in selected_topics}

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
                        progress=on_file_done)
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
                    4, on_dl)
            prog.progress(1.0, text="✅ 完了")
            ss.result_files = files
            ss.result_log = log
            if files:
                st.balloons()
        except Exception as e:
            prog.progress(0.0, text="❌ 失敗")
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
