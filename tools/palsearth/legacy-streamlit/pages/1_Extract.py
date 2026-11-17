import streamlit as st
import sys
import os
import json
import tempfile
import zipfile
import pandas as pd

sys.path.insert(0, '/home/USER/PRECISE/palsearth')

st.set_page_config(page_title="Extract — PALs Earth",
                   page_icon="🗺️", layout="wide")

from core.ui import inject_css, render_sidebar, render_chat_fab
inject_css()

# Auth guard
from core.auth import restore_session
restore_session()
if "username" not in st.session_state or not st.session_state["username"]:
    st.switch_page("app.py")

render_sidebar(active="extract")
username = st.session_state["username"]
JOBS_DIR = "/home/USER/PRECISE/palsearth/jobs"
os.makedirs(JOBS_DIR, exist_ok=True)

from core.datasets import DATASETS
from core.uploads import stage_uploaded_file
from core.design import CATEGORY_COLORS, dataset_tile

ALL_STATS = ["mean", "min", "max", "p50", "p75", "p95", "p99"]
ee_project = "your-earth-engine-project"

# ── Dataset category config ─────────────────────────────────────────────────
# Built from core.design.CATEGORY_COLORS so every category that actually
# appears in DATASETS gets shown — a hand-maintained list here previously
# left out "Land Surface Temperature", "Population" and "Water", silently
# hiding those datasets (Land Surface Temp, WorldPop, JRC Surface Water)
# from this page entirely.
CATEGORY_ORDER = [
    "Vegetation", "Climate", "Land Surface Temperature", "Air Quality",
    "Terrain", "Soil", "Urban", "Water", "Population", "Socioeconomic",
]
CATEGORIES = [(cat, CATEGORY_COLORS[cat]) for cat in CATEGORY_ORDER]

def detect_lat_lon(df):
    lat_c = ["latitude", "lat", "LAT", "Latitude", "LATITUDE", "y", "Y"]
    lon_c = ["longitude", "lon", "LON", "Longitude", "LONGITUDE",
             "x", "X", "lng", "LNG"]
    lat = next((c for c in lat_c if c in df.columns), None)
    lon = next((c for c in lon_c if c in df.columns), None)
    return lat, lon

# ── Page header ─────────────────────────────────────────────────────────────
st.markdown("""
<div style="margin-bottom:1.5rem;">
  <p class="pe-page-title">🗺️ Extract Geospatial Data</p>
  <p class="pe-page-sub">Upload locations, then either fill in the form yourself or ask
  Shmron AI to do it for you — both run the exact same extraction underneath.</p>
</div>
""", unsafe_allow_html=True)

# ════════════════════════════════════════════════════════════════════════════
# SECTION 1 — UPLOAD (shared by both Manual and Ask Shmron AI)
# ════════════════════════════════════════════════════════════════════════════
st.markdown("""
<div class="pe-section-header">
  <span class="pe-step-num">1</span>
  Upload your locations
</div>
""", unsafe_allow_html=True)

uploaded_file = st.file_uploader(
    "CSV (lat/lon columns) or a zipped shapefile",
    type=["csv", "zip"],
    help="CSV: must have latitude & longitude columns.  "
         "Shapefile: zip the .shp, .dbf, .shx (+ .prj) together.",
    label_visibility="collapsed",
)

input_df_preview = None
staged = st.session_state.get("staged_upload")

if uploaded_file is not None:
    sig = f"{uploaded_file.name}:{uploaded_file.size}"
    if st.session_state.get("staged_upload_sig") != sig:
        try:
            staged = stage_uploaded_file(uploaded_file)
            st.session_state["staged_upload"] = staged
            st.session_state["staged_upload_sig"] = sig
            st.session_state["staged_upload_name"] = uploaded_file.name
        except Exception as e:
            st.error(f"Could not read file: {e}")
            staged = None
    uploaded_file.seek(0)
    geometry_type = staged["geometry_type"] if staged else None

    fname = uploaded_file.name
    if fname.endswith(".csv"):
        try:
            input_df_preview = pd.read_csv(uploaded_file)
            lat_col, lon_col = detect_lat_lon(input_df_preview)
            c1, c2 = st.columns([2, 1])
            with c1:
                coord_txt = (
                    f" · Coords: <b>{lat_col}</b> / <b>{lon_col}</b>"
                    if lat_col and lon_col else ""
                )
                st.markdown(f"""
                <div class="pe-success-block">
                  <strong>CSV loaded</strong> — {len(input_df_preview):,} rows,
                  {len(input_df_preview.columns)} columns{coord_txt}
                </div>
                """, unsafe_allow_html=True)
            if not lat_col or not lon_col:
                st.warning("Could not auto-detect lat/lon. Rename columns to "
                           "`latitude`/`longitude`, `lat`/`lon`, or `y`/`x`.")
            elif geometry_type == "points":
                map_df = input_df_preview[[lat_col, lon_col]].dropna().rename(
                    columns={lat_col: "lat", lon_col: "lon"}
                )
                if not map_df.empty:
                    st.map(map_df, size=6, color="#1f7a4a")
            with st.expander("Preview first 5 rows"):
                st.dataframe(input_df_preview.head(5), use_container_width=True)
        except Exception as e:
            st.error(f"Could not read CSV: {e}")

    elif fname.endswith(".zip"):
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                zip_path = os.path.join(tmpdir, fname)
                with open(zip_path, "wb") as f:
                    f.write(uploaded_file.read())
                with zipfile.ZipFile(zip_path, "r") as z:
                    z.extractall(tmpdir)
                from glob import glob
                shp_files = glob(os.path.join(tmpdir, "**", "*.shp"), recursive=True)
                if not shp_files:
                    st.error("No .shp file found inside the zip.")
                else:
                    import geopandas as gpd
                    gdf = gpd.read_file(shp_files[0])
                    st.markdown(f"""
                    <div class="pe-success-block">
                      <strong>Shapefile loaded</strong> — {len(gdf):,} {geometry_type or 'features'}
                      · CRS: {gdf.crs}
                    </div>
                    """, unsafe_allow_html=True)
                    try:
                        gdf_wgs84 = gdf.to_crs(epsg=4326) if gdf.crs and gdf.crs.to_epsg() != 4326 else gdf
                        pts = gdf_wgs84.geometry.centroid
                        map_df = pd.DataFrame({"lat": pts.y, "lon": pts.x}).dropna()
                        if not map_df.empty:
                            st.map(map_df, size=6, color="#1f7a4a")
                    except Exception:
                        pass  # map preview is a nice-to-have, never block the upload on it
                    with st.expander("Preview attributes (first 5 rows)"):
                        st.dataframe(
                            gdf.drop(columns="geometry", errors="ignore").head(5),
                            use_container_width=True,
                        )
                    input_df_preview = gdf.drop(columns="geometry", errors="ignore").head(5)
        except Exception as e:
            st.error(f"Could not read shapefile: {e}")
else:
    geometry_type = staged["geometry_type"] if staged else None
    if staged:
        st.caption(f"Using previously uploaded file — "
                   f"{staged['n_locations']:,} {staged['geometry_type']} "
                   f"({st.session_state.get('staged_upload_name', 'uploaded file')}).")

st.markdown('<hr class="pe-hr">', unsafe_allow_html=True)

# ════════════════════════════════════════════════════════════════════════════
# SECTION 2 — MODE: Ask Shmron AI, or fill in the form yourself
# ════════════════════════════════════════════════════════════════════════════
st.markdown("""
<div class="pe-section-header">
  <span class="pe-step-num">2</span>
  Choose how to build your extraction
</div>
""", unsafe_allow_html=True)

tab_ai, tab_manual = st.tabs(["  ✨ Ask Shmron AI  ", "  📝 Manual  "])

# ──────────────────────────────────────────────────────────────────────────────
# TAB — ASK SHMRON AI
# ──────────────────────────────────────────────────────────────────────────────
with tab_ai:
    from core.apikey import get_anthropic_key
    api_key = get_anthropic_key()
    anthropic_ok = False
    try:
        import anthropic
        anthropic_ok = True
    except ImportError:
        pass

    if not anthropic_ok:
        st.error("`anthropic` package not installed. Run `pip install anthropic`.")
    elif not api_key:
        st.error("No `ANTHROPIC_API_KEY` found. Contact the admin.")
    else:
        from core.ai_agent import run_agent_turn

        if "chat_history" not in st.session_state:
            st.session_state["chat_history"] = []
        if "chat_display" not in st.session_state:
            st.session_state["chat_display"] = []

        if not staged:
            st.caption("Upload a file above, then ask Shmron AI what you need — "
                       "or ask questions without uploading anything first.")

        # ── Welcome / empty state — suggestions are real buttons, click to ask ──
        if not st.session_state["chat_display"]:
            st.caption("Tell it what you're studying, or click one:")
            _suggestions = [
                "What's the difference between wet bulb and land surface temp?",
                "Pull temperature and rainfall for my uploaded villages",
                "I need PM2.5 for a heat-and-air-quality study — what's available?",
                "Can you get flood/burned-area data even though it's not in the list?",
            ]
            _cols = st.columns(2)
            for _i, _sug in enumerate(_suggestions):
                with _cols[_i % 2]:
                    if st.button(_sug, key=f"sugg_{_i}", use_container_width=True):
                        st.session_state["pending_prompt"] = _sug
                        st.rerun()
            st.markdown(" ")

        # ── Chat history (display only) ─────────────────────────────────────────
        def _render_events(events):
            for ev in events:
                if ev["type"] == "text":
                    st.markdown(ev["content"])
                else:
                    st.markdown(f"""
                    <div style="background:#f8f9fa;border:1px solid #e8eaed;
                                border-radius:8px;padding:.45rem .7rem;
                                margin:.3rem 0;font-size:.82rem;color:#3c4043;">
                      {ev['content']}
                    </div>
                    """, unsafe_allow_html=True)

        for turn in st.session_state["chat_display"]:
            with st.chat_message(turn["role"]):
                if turn["role"] == "user":
                    st.markdown(turn["text"])
                else:
                    _render_events(turn["events"])

        # ── One-click plan card — Shmron drafted a plan, not yet submitted ──────
        pending_plan = st.session_state.get("pending_plan")
        if pending_plan:
            from core.datasets import DATASETS as _catalog
            _custom = st.session_state.get("custom_datasets", {})
            _all_ds = {**_catalog, **_custom}
            ds_chips = "".join(
                f'<span class="pe-chip pe-chip-{"blue" if _all_ds.get(d, {}).get("type") == "timeseries" else "purple"}">{d}</span> '
                for d in pending_plan["dataset_names"]
            )
            stat_chips = "".join(f'<span class="pe-chip pe-chip-gray">{s}</span> ' for s in pending_plan.get("stats", []))
            if pending_plan.get("date_mode") == "pregnancy_windows":
                date_row = f"Windows: {', '.join(pending_plan.get('window_keys', []))}"
            elif pending_plan.get("start_date"):
                date_row = f"{pending_plan['start_date']} → {pending_plan.get('end_date', '')}"
            else:
                date_row = "No date range needed (static only)"

            st.markdown(f"""
            <div style="border:1.5px solid #b8f35a;background:#f6fbe9;border-radius:12px;
                        padding:1rem 1.1rem;margin:.5rem 0 .75rem;">
              <div style="font-weight:700;font-size:.88rem;color:#0b2a1d;margin-bottom:.5rem;">
                📝 Extraction plan ready
              </div>
              <div style="font-size:.82rem;margin-bottom:.4rem;">{ds_chips}</div>
              <div style="font-size:.79rem;color:#5d6b62;">
                {date_row} &nbsp;·&nbsp; {stat_chips} &nbsp;·&nbsp;
                {pending_plan.get('output_format', 'csv').upper()}
              </div>
            </div>
            """, unsafe_allow_html=True)

            c_run, c_dismiss = st.columns([1, 3])
            with c_run:
                run_clicked = st.button("▶ Run this extraction", type="primary", key="run_pending_plan")
            with c_dismiss:
                dismiss_clicked = st.button("Dismiss", key="dismiss_pending_plan")

            if run_clicked:
                from core.ai_agent import execute_plan
                result = execute_plan(pending_plan)
                st.session_state.pop("pending_plan", None)
                if result.get("ok"):
                    msg = f"🚀 Job submitted (id `{result['job_id'][:8]}…`) — running in the background. Track it in **My Jobs**."
                else:
                    msg = f"⚠️ Couldn't submit — {result.get('message', '')}"
                st.session_state["chat_display"].append(
                    {"role": "assistant", "events": [{"type": "tool", "content": msg}]}
                )
                st.rerun()
            if dismiss_clicked:
                st.session_state.pop("pending_plan", None)
                st.rerun()

        # ── Input ──────────────────────────────────────────────────────────────
        user_input = st.chat_input("Ask Shmron AI, or tell it what data you need…")
        if not user_input and st.session_state.get("pending_prompt"):
            user_input = st.session_state.pop("pending_prompt")

        if user_input:
            st.session_state["chat_history"].append({"role": "user", "content": user_input})
            st.session_state["chat_display"].append({"role": "user", "text": user_input})
            with st.chat_message("user"):
                st.markdown(user_input)

            with st.chat_message("assistant"):
                with st.spinner(""):
                    try:
                        client = anthropic.Anthropic(api_key=api_key)
                        events = run_agent_turn(client, st.session_state["chat_history"])
                        _render_events(events)
                        st.session_state["chat_display"].append({"role": "assistant", "events": events})
                    except Exception as e:
                        err = f"Shmron AI error: {e}"
                        st.error(err)
                        st.session_state["chat_display"].append(
                            {"role": "assistant", "events": [{"type": "text", "content": err}]}
                        )

            # The plan-card block above (in reading order) runs BEFORE this input
            # handler on every script pass, so a plan drafted just now wouldn't
            # show its "Run this extraction" button until some other rerun
            # happened to fire — which nothing was doing. Force that next pass.
            if st.session_state.get("pending_plan"):
                st.rerun()

        # ── Clear button ───────────────────────────────────────────────────────
        if st.session_state.get("chat_display"):
            st.markdown(" ")
            if st.button("Clear conversation", key="clear_chat"):
                st.session_state["chat_history"] = []
                st.session_state["chat_display"] = []
                st.session_state.pop("pending_plan", None)
                st.rerun()

# ──────────────────────────────────────────────────────────────────────────────
# TAB — MANUAL
# ──────────────────────────────────────────────────────────────────────────────
with tab_manual:
    col_ds, col_cfg = st.columns([3, 2], gap="large")

    # ── Left: Dataset browser ──────────────────────────────────────────────────
    with col_ds:
        st.markdown("""
        <div class="pe-section-header">
          <span class="pe-step-num">3</span>
          Choose datasets
        </div>
        """, unsafe_allow_html=True)

        selected_datasets = []
        ghsl_years = {}  # {dataset_name: chosen_year} for datasets with a "years" epoch picker

        for cat_name, cat_color in CATEGORIES:
            ds_in_cat = [
                name for name, info in DATASETS.items()
                if info.get("category") == cat_name
            ]
            if not ds_in_cat:
                continue

            st.markdown(f"""
            <div style="display:flex;align-items:center;gap:.5rem;margin:1rem 0 .5rem 0;">
              <span style="width:8px;height:8px;border-radius:50%;background:{cat_color};flex-shrink:0;"></span>
              <span style="font-weight:700;font-size:.78rem;color:#202124;
                           letter-spacing:.04em;text-transform:uppercase;">
                {cat_name}
              </span>
            </div>
            """, unsafe_allow_html=True)

            for ds_name in ds_in_cat:
                info = DATASETS[ds_name]
                is_ts = info["type"] == "timeseries"
                badge_cls = "pe-chip-blue" if is_ts else "pe-chip-purple"
                badge_txt = "Timeseries" if is_ts else "Static"
                code, tile_color = dataset_tile(ds_name, cat_name)

                c_chk, c_card = st.columns([1, 15], gap="small")
                with c_chk:
                    st.markdown('<div style="margin-top:.55rem;"></div>', unsafe_allow_html=True)
                    checked = st.checkbox(
                        ds_name, key=f"ds__{ds_name}", label_visibility="collapsed",
                    )
                with c_card:
                    border = tile_color if checked else "#e2ddd0"
                    fill = f"{tile_color}1a" if checked else "#ffffff"
                    st.markdown(f"""
                    <div style="display:flex;align-items:flex-start;gap:.6rem;
                                padding:.5rem .75rem;border-radius:10px;
                                border:1.5px solid {border};background:{fill};
                                margin-bottom:.35rem;">
                      <span class="pe-ds-tile" style="background:{tile_color};">{code}</span>
                      <div style="flex:1;min-width:0;">
                        <div style="display:flex;align-items:center;gap:.4rem;flex-wrap:wrap;">
                          <span style="font-weight:600;font-size:.85rem;color:#1a1f1b;">{ds_name}</span>
                          <span class="pe-chip {badge_cls}">{badge_txt}</span>
                        </div>
                        <div style="font-size:.76rem;color:#80868b;margin-top:1px;line-height:1.4;">
                          {info['description']}
                        </div>
                      </div>
                    </div>
                    """, unsafe_allow_html=True)

                if checked:
                    selected_datasets.append(ds_name)
                    if "years" in info:
                        years = info["years"]
                        default_idx = years.index(2025) if 2025 in years else len(years) - 1
                        chosen_year = st.selectbox(
                            f"{ds_name} — epoch",
                            options=years,
                            index=default_idx,
                            key=f"year__{ds_name}",
                            label_visibility="collapsed",
                            help=f"Which {ds_name} epoch to pull",
                        )
                        ghsl_years[ds_name] = chosen_year

        if not selected_datasets:
            st.markdown("""
            <div style="margin-top:.5rem;padding:.75rem 1rem;background:#f8f9fa;
                        border-radius:8px;font-size:.83rem;color:#9aa0a6;
                        border:1px dashed #dadce0;">
              Tick one or more datasets above to include them in the extraction.
            </div>
            """, unsafe_allow_html=True)

    # ── Right: Parameters ──────────────────────────────────────────────────────
    with col_cfg:
        st.markdown("""
        <div class="pe-section-header">
          <span class="pe-step-num">4</span>
          Parameters
        </div>
        """, unsafe_allow_html=True)

        has_ts = any(
            DATASETS[d]["type"] == "timeseries"
            for d in selected_datasets
        ) if selected_datasets else False

        start_date = end_date = None

        if has_ts:
            st.markdown("**Date range** *(timeseries datasets)*")
            import datetime
            default_end   = datetime.date.today()
            default_start = default_end - datetime.timedelta(days=30)
            c_s, c_e = st.columns(2)
            with c_s:
                start_str = st.text_input("Start", value=str(default_start),
                                          placeholder="YYYY-MM-DD",
                                          key="start_date")
            with c_e:
                end_str = st.text_input("End", value=str(default_end),
                                        placeholder="YYYY-MM-DD",
                                        key="end_date")
            try:
                start_date = datetime.date.fromisoformat(start_str)
                end_date   = datetime.date.fromisoformat(end_str)
                if start_date > end_date:
                    st.error("Start must be before end date.")
                    start_date = end_date = None
                else:
                    n_days = (end_date - start_date).days + 1
                    st.markdown(f"""
                    <div class="pe-info-block" style="margin-top:.3rem;">
                      {n_days:,} day{'s' if n_days != 1 else ''} selected
                    </div>
                    """, unsafe_allow_html=True)
            except ValueError:
                st.error("Use YYYY-MM-DD format.")
                start_date = end_date = None
            st.markdown(" ")

        st.markdown("**Statistics**")
        selected_stats = st.pills(
            "Statistics",
            options=ALL_STATS,
            default=["mean"],
            selection_mode="multi",
            help="Applied to each extracted band. Timeseries-only stats (p50–p99) are ignored for static datasets.",
            label_visibility="collapsed",
        ) or []

        st.markdown(" ")
        st.markdown("**Output format**")
        output_format = st.radio(
            "Format", ["csv", "parquet"], index=0, horizontal=True,
            help="CSV: universal. Parquet: faster for large datasets.",
            label_visibility="collapsed",
        )

        if selected_datasets:
            st.markdown(" ")
            has_static  = any(DATASETS[d]["type"] != "timeseries" for d in selected_datasets)
            has_dynamic = has_ts
            type_chips = ""
            if has_dynamic:
                type_chips += '<span class="pe-chip pe-chip-blue">Timeseries</span> '
            if has_static:
                type_chips += '<span class="pe-chip pe-chip-purple">Static</span> '
            ds_count = len(selected_datasets)
            st.markdown(f"""
            <div style="padding:.65rem .9rem;background:#f6fef7;
                        border:1px solid #b7dfbf;border-radius:8px;
                        font-size:.82rem;">
              <strong style="color:#0d5c2e;">{ds_count} dataset{'s' if ds_count!=1 else ''}</strong>
              selected &nbsp; {type_chips}
            </div>
            """, unsafe_allow_html=True)

    st.markdown('<hr class="pe-hr">', unsafe_allow_html=True)

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 4 — REVIEW & SUBMIT
    # ════════════════════════════════════════════════════════════════════════
    st.markdown("""
    <div class="pe-section-header">
      <span class="pe-step-num">5</span>
      Review &amp; submit
    </div>
    """, unsafe_allow_html=True)

    can_submit = True
    issues = []

    if not staged:
        can_submit = False
        issues.append("No file uploaded.")
    if not selected_datasets:
        can_submit = False
        issues.append("No datasets selected.")
    if has_ts and (not start_date or not end_date):
        can_submit = False
        issues.append("Date range required for timeseries datasets.")
    if not selected_stats:
        can_submit = False
        issues.append("Select at least one statistic.")

    if can_submit and staged:
        date_row = ""
        if has_ts and start_date and end_date:
            n = (end_date - start_date).days + 1
            date_row = (
                f'<div class="sr"><span class="sl">Date range</span>'
                f'{start_date} → {end_date} ({n:,} days)</div>'
            )
        datasets_chips = "".join(
            f'<span class="pe-chip pe-chip-{"blue" if DATASETS[d]["type"]=="timeseries" else "purple"}">{d}</span> '
            for d in selected_datasets
        )
        stats_chips = "".join(
            f'<span class="pe-chip pe-chip-gray">{s}</span> '
            for s in selected_stats
        )
        st.markdown(f"""
        <div class="pe-summary">
          <div class="sr">
            <span class="sl">File</span>
            {st.session_state.get('staged_upload_name', 'uploaded file')}
            <span class="pe-chip pe-chip-gray" style="margin-left:6px;">{staged['geometry_type']}</span>
          </div>
          <div class="sr"><span class="sl">Datasets</span>{datasets_chips}</div>
          <div class="sr"><span class="sl">Stats</span>{stats_chips}</div>
          <div class="sr">
            <span class="sl">Format</span>
            <span class="pe-chip pe-chip-green">{output_format.upper()}</span>
          </div>
          {date_row}
        </div>
        """, unsafe_allow_html=True)

    for issue in issues:
        st.warning(issue)

    submit_btn = st.button(
        "Submit extraction job",
        type="primary",
        disabled=not can_submit,
        key="manual_submit",
    )

    if submit_btn and can_submit and staged:
        from core.jobs_db import create_job
        from core.worker import run_job_in_background
        from core.datasets import ghsl_for_year

        custom_datasets = {
            ds_name: ghsl_for_year(year)
            for ds_name, year in ghsl_years.items()
            if ds_name in selected_datasets
        }

        job_id = create_job(
            username=username,
            datasets=selected_datasets,
            start_date=str(start_date) if start_date else None,
            end_date=str(end_date) if end_date else None,
            geometry_type=staged["geometry_type"],
            input_filename=staged["input_filename"],
            ee_project=ee_project,
            stats_requested=selected_stats,
            output_format=output_format,
            custom_datasets=custom_datasets,
        )
        run_job_in_background(job_id)

        st.success(f"Job submitted — ID: **{job_id[:8]}…**")
        st.markdown("""
        <div class="pe-info-block">
          Extraction is running in the background.
          Go to <strong>My Jobs</strong> to track progress and download results.
        </div>
        """, unsafe_allow_html=True)
        st.page_link("pages/2_My_Jobs.py", label="Track in My Jobs →", icon="📋")
