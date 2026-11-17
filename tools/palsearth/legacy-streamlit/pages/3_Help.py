import streamlit as st
import sys
import os

sys.path.insert(0, '/home/USER/PRECISE/palsearth')

st.set_page_config(page_title="Dataset Reference — PALs Earth",
                   page_icon="📦", layout="wide")

from core.ui import inject_css, render_sidebar, render_chat_fab
inject_css()

# Auth guard
from core.auth import restore_session
restore_session()
if "username" not in st.session_state or not st.session_state["username"]:
    st.switch_page("app.py")

render_sidebar(active="help")

from core.datasets import DATASETS

# ── Page header ────────────────────────────────────────────────────────────────
st.markdown("""
<p class="pe-page-title">📦 Dataset Reference</p>
<p class="pe-page-sub">Everything available for extraction, via Google Earth Engine —
descriptions, collections, and how to use PALs Earth. To actually run an extraction,
head to <strong>Extract Data</strong>.</p>
""", unsafe_allow_html=True)

# ── Quick usage guide ──────────────────────────────────────────────────────────
st.markdown("""
<div class="pe-card" style="margin-bottom:1.25rem;">
  <div style="font-weight:600;font-size:.97rem;margin-bottom:.85rem;color:#202124;">
    ⚡ How to use PALs Earth
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:.75rem;">
    <div style="background:#f8f9fa;border-radius:10px;padding:.875rem 1rem;">
      <div style="font-weight:600;font-size:.85rem;color:#0d5c2e;margin-bottom:.3rem;">
        1 · Upload locations
      </div>
      <div style="font-size:.81rem;color:#5f6368;line-height:1.5;">
        CSV with <code>latitude</code> / <code>longitude</code> columns,
        or a zipped shapefile (.shp + .dbf + .shx).
      </div>
    </div>
    <div style="background:#f8f9fa;border-radius:10px;padding:.875rem 1rem;">
      <div style="font-weight:600;font-size:.85rem;color:#0d5c2e;margin-bottom:.3rem;">
        2 · Pick datasets &amp; stats — yourself, or ask Shmron AI
      </div>
      <div style="font-size:.81rem;color:#5f6368;line-height:1.5;">
        Fill in the manual form, or describe what you're studying in plain
        language and let Shmron AI pick datasets and submit for you.
      </div>
    </div>
    <div style="background:#f8f9fa;border-radius:10px;padding:.875rem 1rem;">
      <div style="font-weight:600;font-size:.85rem;color:#0d5c2e;margin-bottom:.3rem;">
        3 · Submit &amp; wait
      </div>
      <div style="font-size:.81rem;color:#5f6368;line-height:1.5;">
        Jobs run in the background. Visit <strong>My Jobs</strong> to track
        progress — the page auto-refreshes every 10 seconds.
      </div>
    </div>
    <div style="background:#f8f9fa;border-radius:10px;padding:.875rem 1rem;">
      <div style="font-weight:600;font-size:.85rem;color:#0d5c2e;margin-bottom:.3rem;">
        4 · Download results
      </div>
      <div style="font-size:.81rem;color:#5f6368;line-height:1.5;">
        Output is wide-format: <code>date</code> + your location attributes +
        one column per extracted variable. Available as CSV or Parquet.
      </div>
    </div>
  </div>
</div>
""", unsafe_allow_html=True)

# ── Dataset table ──────────────────────────────────────────────────────────────
st.markdown("""
<div style="font-weight:600;font-size:.97rem;margin-bottom:.85rem;color:#202124;">
  📦 Available datasets
</div>
""", unsafe_allow_html=True)

from core.design import dataset_tile

rows_html = ""
for name, info in DATASETS.items():
    is_ts       = info["type"] == "timeseries"
    badge_cls   = "pe-chip-blue" if is_ts else "pe-chip-purple"
    badge_label = "Timeseries" if is_ts else "Static"
    code, bg    = dataset_tile(name, info.get("category", ""))
    band_disp   = (
        ", ".join(info["band"]) if isinstance(info["band"], list)
        else info["band"]
    )
    # Single line, no blank lines between rows — st.markdown's HTML-block mode
    # (unsafe_allow_html=True) terminates at the first blank line it sees, and
    # anything after that falls back to plain markdown, where 4+ leading spaces
    # (from a multi-line indented template) get read as a code block and the
    # raw tags print as literal text instead of rendering. Bit us on this exact
    # loop before — every row must stay one continuous line.
    rows_html += (
        f'<div class="docs-ds-row">'
        f'<div class="pe-ds-tile" style="background:{bg};width:auto;min-width:38px;height:38px;border-radius:10px;font-size:.72rem;">{code}</div>'
        f'<div style="flex:1;min-width:0;">'
        f'<div style="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;">'
        f'<span class="docs-ds-name">{name}</span>'
        f'<span class="pe-chip {badge_cls}">{badge_label}</span>'
        f'</div>'
        f'<div class="docs-ds-desc">{info["description"]}</div>'
        f'<div class="docs-ds-meta">collection: {info["collection"]} &nbsp;·&nbsp; '
        f'band: {band_disp} &nbsp;·&nbsp; scale: {info["scale"]} m</div>'
        f'</div>'
        f'</div>'
    )

st.markdown(f"""
<div class="docs-card">{rows_html}</div>
""", unsafe_allow_html=True)

# ── Notes ──────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="pe-info-block" style="margin-top:1rem;">
  <strong>Notes:</strong>
  CHIRPS has a ~2-month data lag — recent dates will return empty values.
  iSDAsoil datasets cover Africa only.
  All extractions use the PALs Earth service account; no personal Earth Engine
  credentials are needed.
</div>
""", unsafe_allow_html=True)

render_chat_fab()
