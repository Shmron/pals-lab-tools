"""PALs Earth – Design System"""
import streamlit as st

_FONT_IMPORT = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400..800&family=Geist:wght@400;500;600;700;800&display=swap" rel="stylesheet">
"""

GLOBAL_CSS = _FONT_IMPORT + """
<style>

/* ══ Base & reset ════════════════════════════════════════════════════════════ */
*, *::before, *::after { box-sizing: border-box; }

html, body, [class*="css"], [class*="st-"] {
  font-family: 'Geist', ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif !important;
  color: #1a1f1b;
}

/* Display font for headings — set explicitly per element below since the
   broad rule above (with !important) would otherwise win over any inherited
   heading font-family. */
.pe-heading, .pe-page-title, .auth-title, .pe-hero-title, .sb-title, h1, h2, h3 {
  font-family: 'Bricolage Grotesque', 'Geist', sans-serif !important;
}

/* The broad rule above also catches Streamlit's Material Symbols icon glyphs
   (expander chevrons, selectbox/multiselect arrows, alert icons, etc.) — those
   render via font ligatures (e.g. "keyboard_arrow_right" as literal text that
   the icon font turns into a glyph), so overriding their font-family breaks
   them into overlapping plain text. Restore the icon font specifically. */
[data-testid="stIconMaterial"] {
  font-family: 'Material Symbols Rounded' !important;
}

.stApp { background: #f6f4ee !important; }

/* ══ Primary buttons — brand lime, replacing Streamlit's default red ══════════ */
button[kind="primary"], [data-testid^="stBaseButton-primary"] {
  background: #b8f35a !important;
  border: 1px solid #b8f35a !important;
  color: #0b2a1d !important;
  font-weight: 700 !important;
}
button[kind="primary"]:hover, [data-testid^="stBaseButton-primary"]:hover {
  background: #c9f77a !important;
  border-color: #c9f77a !important;
  color: #0b2a1d !important;
}
button[kind="primary"]:disabled, [data-testid^="stBaseButton-primary"]:disabled {
  background: #e2ddd0 !important;
  border-color: #e2ddd0 !important;
  color: #8a948d !important;
}

/* ══ Pills widget (stats toggles) — selected state was Streamlit's default red ═ */
[data-testid="stBaseButton-pillsActive"] {
  background: #b8f35a !important;
  border-color: #b8f35a !important;
  color: #0b2a1d !important;
}
.main .block-container { max-width: 1100px; padding: 2rem 2.25rem 6rem; }
#MainMenu, footer { visibility: hidden; }
header[data-testid="stHeader"] { background: transparent !important; box-shadow: none !important; }

/* ══ Hide Streamlit auto-nav ═════════════════════════════════════════════════ */
[data-testid="stSidebarNav"],
[data-testid="stSidebarNavItems"],
[data-testid="stSidebarNavSeparator"],
[data-testid="stSidebarNavLink"],
section[data-testid="stSidebar"] nav { display: none !important; }

/* ══ Collapsible toggle ══════════════════════════════════════════════════════ */
[data-testid="collapsedControl"] button span,
[data-testid="collapsedControl"] span {
  overflow: hidden !important; max-width: 2rem !important; white-space: nowrap !important;
}

/* ══ Sidebar — dark forest green ════════════════════════════════════════════ */
[data-testid="stSidebar"] {
  background: #0b2a1d !important;
  border-right: 1px solid #091510 !important;
  box-shadow: 2px 0 16px rgba(0,0,0,.18) !important;
}
[data-testid="stSidebarContent"] { padding: 1.5rem 1rem !important; }

/* Page links inside dark sidebar */
[data-testid="stSidebar"] [data-testid="stPageLink"] {
  border-radius: 9px !important;
  margin: 1px 0 !important;
}
[data-testid="stSidebar"] [data-testid="stPageLink"] > div {
  border-radius: 9px !important;
  padding: .55rem .75rem !important;
  transition: background .15s !important;
}
[data-testid="stSidebar"] [data-testid="stPageLink"] > div:hover {
  background: rgba(255,255,255,.07) !important;
}
[data-testid="stSidebar"] [data-testid="stPageLink"] p,
[data-testid="stSidebar"] [data-testid="stPageLink"] span,
[data-testid="stSidebar"] [data-testid="stPageLink"] a {
  color: #a7c4ae !important;
  font-size: .875rem !important;
  font-weight: 500 !important;
  text-decoration: none !important;
}
[data-testid="stSidebar"] [data-testid="stPageLink"] > div[aria-current="page"] {
  background: rgba(184,243,90,.14) !important;
}
[data-testid="stSidebar"] [data-testid="stPageLink"] > div[aria-current="page"] p,
[data-testid="stSidebar"] [data-testid="stPageLink"] > div[aria-current="page"] span {
  color: #b8f35a !important;
  font-weight: 600 !important;
}

/* Sign-out button in dark sidebar — was near-invisible: the base global rule
   (html, body, [class*="css"], [class*="st-"] { color: #1a1f1b }) matches the
   button's inner <p> directly and wins over color set on the <button> itself,
   since color set on a parent only applies via inheritance, not override.
   Must target the inner text explicitly, not just the button. */
[data-testid="stSidebar"] [data-testid="stButton"] button {
  background: rgba(255,255,255,.05) !important;
  border: 1px solid rgba(255,255,255,.24) !important;
  border-radius: 9px !important;
  transition: background .14s, border-color .14s !important;
}
[data-testid="stSidebar"] [data-testid="stButton"] button p {
  color: #cfe4d2 !important;
  font-size: .84rem !important;
  font-weight: 500 !important;
  transition: color .14s !important;
}
[data-testid="stSidebar"] [data-testid="stButton"] button:hover {
  background: rgba(255,255,255,.1) !important;
  border-color: rgba(255,255,255,.36) !important;
}
[data-testid="stSidebar"] [data-testid="stButton"] button:hover p {
  color: #ffffff !important;
}

/* ══ Sidebar brand & user ════════════════════════════════════════════════════ */
.sb-brand {
  display: flex; align-items: center; gap: .75rem;
  padding: 0 .25rem 1.25rem;
  border-bottom: 1px solid rgba(255,255,255,.07);
  margin-bottom: 1rem;
}
.sb-icon {
  width: 40px; height: 40px;
  background: linear-gradient(135deg, #166534 0%, #4ade80 100%);
  border-radius: 12px;
  display: flex; align-items: center; justify-content: center;
  font-size: 1.3rem; flex-shrink: 0;
  box-shadow: 0 4px 12px rgba(74,222,128,.25);
}
.sb-title { font-size: 1rem; font-weight: 700; color: #f0fdf4; letter-spacing: -.01em; }
.sb-subtitle { font-size: .68rem; color: #4d7a56; margin-top: 1px; }

.sb-user {
  display: flex; align-items: center; gap: .55rem;
  padding: .5rem .75rem;
  background: rgba(255,255,255,.04);
  border: 1px solid rgba(255,255,255,.07);
  border-radius: 10px;
  margin-bottom: .5rem;
}
.sb-avatar {
  width: 28px; height: 28px;
  background: linear-gradient(135deg, #166534, #22c55e);
  border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-size: .75rem; font-weight: 700; flex-shrink: 0;
}
.sb-uname { font-size: .83rem; font-weight: 600; color: #d1fae5; }
.sb-urole { font-size: .67rem; color: #4d7a56; margin-top: 1px; }
.sb-divider { border: none; border-top: 1px solid rgba(255,255,255,.06); margin: .75rem 0; }
.sb-section-label {
  font-size: .64rem; font-weight: 600; letter-spacing: .08em;
  text-transform: uppercase; color: #3a5c42;
  padding: 0 .5rem; margin-bottom: .3rem; display: block;
}

/* ══ Typography ═════════════════════════════════════════════════════════════ */
h1, h2, h3, h4 { font-weight: 700 !important; color: #0f1f12 !important; }
p, li, td, th { line-height: 1.6; }

/* ══ Cards ═══════════════════════════════════════════════════════════════════ */
.pe-card {
  background: #fff;
  border-radius: 14px;
  border: 1px solid #e2e8e2;
  padding: 1.5rem 1.75rem;
  box-shadow: 0 1px 3px rgba(0,0,0,.06), 0 1px 2px rgba(0,0,0,.04);
  margin-bottom: 1rem;
  transition: box-shadow .18s, transform .18s;
}
.pe-card:hover { box-shadow: 0 4px 14px rgba(0,0,0,.1); transform: translateY(-1px); }

/* ══ Page header ════════════════════════════════════════════════════════════ */
.pe-page-header { margin-bottom: 1.75rem; }
.pe-page-title {
  font-size: 1.5rem; font-weight: 700; color: #0f1f12;
  margin: 0 0 .2rem; letter-spacing: -.02em;
}
.pe-page-sub { font-size: .9rem; color: #637066; margin: 0; }

/* ══ Steps / Sections ═══════════════════════════════════════════════════════ */
.pe-step-header, .pe-section-header {
  display: flex; align-items: center; gap: .75rem;
  padding: .8rem 1.1rem;
  background: #fff; border: 1px solid #e2e8e2;
  border-radius: 10px; margin-bottom: 1rem;
  font-weight: 600; font-size: .93rem; color: #1a1f1b;
  box-shadow: 0 1px 2px rgba(0,0,0,.04);
}
.pe-step-num {
  width: 26px; height: 26px;
  background: #1f7a4a; color: #fff;
  border-radius: 7px;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: .78rem; font-weight: 700; flex-shrink: 0;
}
.pe-step-done .pe-step-num { background: #123f2b; }
.pe-step-done { border-color: #cfd9c8; background: #f0f7ea; }

/* ══ Dataset code tile — replaces emoji icons with a coloured short-code ═══ */
.pe-ds-tile {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 26px; height: 26px; padding: 0 6px;
  border-radius: 7px; color: #fff;
  font-size: .68rem; font-weight: 800; letter-spacing: .01em;
  flex-shrink: 0;
}

/* ══ Chips / Badges ═════════════════════════════════════════════════════════ */
.pe-chip {
  display: inline-flex; align-items: center; gap: 3px;
  padding: 2px 9px; border-radius: 6px;
  font-size: .72rem; font-weight: 600; letter-spacing: .01em;
}
.pe-chip-green  { background: #dcfce7; color: #15803d; }
.pe-chip-blue   { background: #dbeafe; color: #1d4ed8; }
.pe-chip-purple { background: #f3e8ff; color: #7e22ce; }
.pe-chip-orange { background: #fef3c7; color: #b45309; }
.pe-chip-red    { background: #fee2e2; color: #b91c1c; }
.pe-chip-gray   { background: #f3f4f6; color: #4b5563; }

/* Status dots */
.pe-dot {
  display: inline-block; width: 7px; height: 7px;
  border-radius: 50%; margin-right: 5px; vertical-align: middle; flex-shrink: 0;
}
.pe-dot-green { background: #16a34a; }
.pe-dot-blue  { background: #2563eb; animation: blink .9s ease-in-out infinite; }
.pe-dot-red   { background: #dc2626; }
.pe-dot-gray  { background: #9ca3af; }
@keyframes blink {
  0%,100% { opacity: 1; transform: scale(1); }
  50%      { opacity: .4; transform: scale(1.3); }
}

/* ══ Progress bar ════════════════════════════════════════════════════════════ */
.pe-prog-track {
  width: 100%; height: 5px; background: #e5e7eb;
  border-radius: 3px; overflow: hidden; margin: .5rem 0;
}
.pe-prog-bar {
  height: 100%;
  background: linear-gradient(90deg, #16a34a, #4ade80);
  border-radius: 3px; transition: width .4s ease;
}
.pe-prog-bar.anim {
  background-size: 200% 100%;
  animation: shimmer 1.6s linear infinite;
  background-image: linear-gradient(90deg, #16a34a 0%, #4ade80 50%, #16a34a 100%);
}
@keyframes shimmer { from { background-position: 200% 0; } to { background-position: -200% 0; } }

/* ══ Dataset cards ══════════════════════════════════════════════════════════ */
.pe-ds-card {
  background: #fff; border: 1.5px solid #e2e8e2; border-radius: 10px;
  padding: .8rem 1rem; margin-bottom: .45rem;
  transition: border-color .14s, box-shadow .14s;
}
.pe-ds-card:hover { border-color: #16a34a; box-shadow: 0 0 0 3px rgba(22,163,74,.08); }
.pe-ds-card .ds-name { font-weight: 600; font-size: .9rem; color: #1a1f1b; }
.pe-ds-card .ds-desc { font-size: .79rem; color: #637066; margin-top: 2px; }
.pe-ds-card .ds-meta { font-size: .74rem; color: #9ca3af; margin-top: 4px; }

/* ══ Job cards ══════════════════════════════════════════════════════════════ */
.pe-job-card {
  background: #fff; border: 1px solid #e2e8e2; border-radius: 14px;
  padding: 1.1rem 1.35rem; margin-bottom: .85rem;
  box-shadow: 0 1px 3px rgba(0,0,0,.06); transition: box-shadow .18s;
}
.pe-job-card:hover { box-shadow: 0 4px 14px rgba(0,0,0,.1); }
.jc-row  { display: flex; align-items: center; justify-content: space-between; }
.jc-id   { font-weight: 700; font-size: .93rem; color: #1a1f1b; }
.jc-ts   { font-size: .76rem; color: #9ca3af; }
.jc-ds   { font-size: .8rem; color: #637066; margin-top: .3rem; }
.jc-meta { font-size: .76rem; color: #9ca3af; margin-top: .25rem; }
.jc-chips { display: flex; flex-wrap: wrap; gap: .3rem; margin-top: .45rem; }

/* ══ Summary & alerts ═══════════════════════════════════════════════════════ */
.pe-summary {
  background: #f0fdf4; border: 1.5px solid #86efac;
  border-radius: 12px; padding: 1.1rem 1.4rem; margin: 1rem 0;
}
.pe-summary .sr { margin-bottom: .28rem; font-size: .86rem; }
.pe-summary .sl { font-weight: 600; color: #166534; margin-right: .4rem; }

.pe-info-block {
  background: #eff6ff; border-left: 3px solid #2563eb;
  border-radius: 0 8px 8px 0; padding: .7rem 1rem;
  font-size: .84rem; color: #1e3a5f; margin-bottom: 1rem;
}
.pe-success-block {
  background: #f0fdf4; border-left: 3px solid #16a34a;
  border-radius: 0 8px 8px 0; padding: .7rem 1rem;
  font-size: .84rem; color: #14532d; margin-bottom: 1rem;
}

/* ══ Stats tiles ═════════════════════════════════════════════════════════════ */
.stats-row { display: flex; gap: .75rem; flex-wrap: wrap; margin-bottom: 1.5rem; }
.stat-tile {
  flex: 1; min-width: 120px;
  background: #fff; border: 1px solid #e2e8e2;
  border-radius: 12px; padding: 1rem 1.1rem;
  text-align: center; box-shadow: 0 1px 2px rgba(0,0,0,.04);
}
.st-val   { font-size: 1.6rem; font-weight: 700; color: #166534; }
.st-label { font-size: .74rem; color: #637066; margin-top: 2px; font-weight: 500; }

/* ══ Auth — catalogue-style card ═════════════════════════════════════════════ */

/* Dark gradient page backdrop (only active on auth page via .auth-bg class) */
.auth-bg { background: linear-gradient(135deg,#0c2218 0%,#16100a 55%,#0a1f2e 100%) !important; }

/* The stContainer(border=True) card */
[data-testid="stVerticalBlockBorderWrapper"] {
  background: #fff !important;
  border-radius: 14px !important;
  border: none !important;
  box-shadow: 0 25px 80px rgba(0,0,0,.45) !important;
  padding: 2.5rem 2.25rem !important;
}

.auth-logo-wrap {
  text-align: center; margin-bottom: 1.5rem;
}
.auth-logo-wrap img {
  width: 92px; height: auto; display: inline-block;
}
.auth-title {
  font-family: 'Bricolage Grotesque', 'Geist', sans-serif !important;
  font-size: 1.85rem; font-weight: 700;
  color: #1a1f1b; letter-spacing: -.01em;
  text-align: center; margin-bottom: .3rem;
}
.auth-tagline { font-size: .88rem; color: #637066; text-align: center; margin-bottom: 0; }

/* ══ Divider ═════════════════════════════════════════════════════════════════ */
.pe-hr { border: none; border-top: 1px solid #e2e8e2; margin: 1.5rem 0; }

/* ══ Docs page ═══════════════════════════════════════════════════════════════ */
.docs-card {
  background: #fff; border: 1px solid #e2e8e2; border-radius: 12px;
  padding: 1rem 1.35rem; margin-bottom: .6rem;
  box-shadow: 0 1px 2px rgba(0,0,0,.04);
}
.docs-ds-row {
  display: flex; align-items: flex-start; gap: 1rem;
  padding: .85rem 0; border-bottom: 1px solid #f1f5f2;
}
.docs-ds-row:last-child { border-bottom: none; }
.docs-ds-icon {
  width: 36px; height: 36px; flex-shrink: 0;
  border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 1.2rem;
}
.docs-ds-name { font-weight: 600; font-size: .88rem; color: #1a1f1b; }
.docs-ds-desc { font-size: .78rem; color: #637066; margin-top: 2px; }
.docs-ds-meta { font-size: .73rem; color: #9ca3af; margin-top: 4px; font-family: monospace; }

/* ══ File uploader ═══════════════════════════════════════════════════════════ */
[data-testid="stFileUploaderDropzoneInstructions"] svg,
[data-testid="stFileUploaderDropzoneInstructions"] > div > span:first-child > svg { display: none !important; }
[data-testid="stFileUploaderDropzone"] {
  border-radius: 12px !important;
  border: 2px dashed #c4cfc5 !important;
  background: #fafcfa !important;
  transition: border-color .15s, background .15s !important;
}
[data-testid="stFileUploaderDropzone"]:hover {
  border-color: #16a34a !important;
  background: #f0fdf4 !important;
}

/* ══ Inputs ══════════════════════════════════════════════════════════════════ */
[data-testid="stTextInput"] input {
  border-radius: 9px !important; border-color: #d1d5db !important;
  font-size: .9rem !important; background: #fafafa !important;
  transition: border-color .14s, box-shadow .14s !important;
}
[data-testid="stTextInput"] input:focus {
  border-color: #16a34a !important;
  box-shadow: 0 0 0 3px rgba(22,163,74,.14) !important;
  background: #fff !important;
}

/* ══ Buttons ═════════════════════════════════════════════════════════════════ */
button[kind="primary"],
.stButton > button[kind="primary"] {
  background: #16a34a !important; border-color: #16a34a !important;
  color: #fff !important; border-radius: 9px !important;
  font-weight: 600 !important; letter-spacing: .01em !important;
  transition: background .15s, box-shadow .15s !important;
}
button[kind="primary"]:hover,
.stButton > button[kind="primary"]:hover {
  background: #15803d !important; border-color: #15803d !important;
  box-shadow: 0 4px 14px rgba(22,163,74,.38) !important;
}

/* ══ Tabs ════════════════════════════════════════════════════════════════════ */
[data-baseweb="tab-list"] { gap: 4px !important; }
[data-baseweb="tab"] {
  border-radius: 8px 8px 0 0 !important;
  font-weight: 500 !important; font-size: .86rem !important;
}
[data-baseweb="tab"][aria-selected="true"] { color: #16a34a !important; }
[data-baseweb="tab-highlight"] { background: #16a34a !important; }

/* ══ Shmron FAB ══════════════════════════════════════════════════════════════ */
.shmron-fab-wrap {
  position: fixed; bottom: 24px; right: 24px;
  z-index: 99999; display: flex; flex-direction: column; align-items: flex-end; gap: 6px;
}
.shmron-fab {
  width: 52px; height: 52px; border-radius: 16px;
  background: linear-gradient(135deg, #15803d 0%, #4ade80 100%);
  box-shadow: 0 6px 20px rgba(22,163,74,.4), 0 2px 6px rgba(0,0,0,.14);
  display: flex; align-items: center; justify-content: center;
  cursor: pointer; text-decoration: none; font-size: 1.4rem;
  border: 2px solid rgba(255,255,255,.28); color: #fff;
  transition: transform .2s cubic-bezier(.34,1.56,.64,1), box-shadow .2s;
  animation: fab-pop .4s cubic-bezier(.34,1.56,.64,1) forwards;
}
.shmron-fab:hover { transform: scale(1.08) translateY(-2px); box-shadow: 0 10px 28px rgba(22,163,74,.5); }
.shmron-fab:active { transform: scale(.95); }
.shmron-fab-tip {
  background: #1a1f1b; color: #fff; padding: 4px 10px; border-radius: 7px;
  font-size: .73rem; white-space: nowrap; opacity: 0;
  transform: translateY(4px) scale(.95); pointer-events: none;
  transition: opacity .16s, transform .16s; box-shadow: 0 2px 8px rgba(0,0,0,.2);
}
.shmron-fab-wrap:hover .shmron-fab-tip { opacity: 1; transform: translateY(0) scale(1); }
@keyframes fab-pop {
  0%   { transform: scale(0) rotate(-15deg); opacity: 0; }
  100% { transform: scale(1) rotate(0deg);   opacity: 1; }
}

/* ══ Home hero ═══════════════════════════════════════════════════════════════ */
.pe-hero {
  background: linear-gradient(140deg, #052e10 0%, #14532d 55%, #166534 100%);
  border-radius: 18px; padding: 2.25rem 2.5rem; margin-bottom: 2rem;
  box-shadow: 0 8px 28px rgba(5,46,16,.3); position: relative; overflow: hidden;
}
.pe-hero::before {
  content: ''; position: absolute; inset: 0;
  background: radial-gradient(ellipse at 78% 20%, rgba(74,222,128,.13) 0%, transparent 60%);
  pointer-events: none;
}
.pe-hero-eyebrow {
  font-size: .7rem; font-weight: 600; letter-spacing: .1em;
  text-transform: uppercase; color: #4ade80; margin-bottom: .6rem;
}
.pe-hero-title {
  font-size: 2rem; font-weight: 700; color: #fff;
  letter-spacing: -.02em; margin: 0 0 .5rem;
  display: flex; align-items: center; gap: .65rem;
}
.pe-hero-sub {
  font-size: .95rem; color: #86efac;
  margin: 0; max-width: 540px; line-height: 1.6;
}

/* ══ Feature cards (home) ════════════════════════════════════════════════════ */
div[data-testid="stColumns"] [data-testid="stButton"] > button {
  background: #fff !important; border: 1.5px solid #e2e8e2 !important;
  border-radius: 16px !important; padding: 1.75rem 1.5rem !important;
  min-height: 190px !important; text-align: center !important;
  box-shadow: 0 1px 4px rgba(0,0,0,.06) !important;
  transition: transform .2s, box-shadow .2s, border-color .15s !important;
  width: 100% !important;
}
div[data-testid="stColumns"] [data-testid="stButton"] > button:hover {
  transform: translateY(-3px) !important;
  box-shadow: 0 8px 24px rgba(0,0,0,.12) !important;
  border-color: #86efac !important;
  background: #fff !important;
}
div[data-testid="stColumns"] [data-testid="stButton"] > button p:nth-child(1) {
  font-size: 2.2rem !important; margin: 0 0 .7rem 0 !important; line-height: 1 !important;
}
div[data-testid="stColumns"] [data-testid="stButton"] > button p:nth-child(2) {
  font-size: .98rem !important; font-weight: 700 !important;
  color: #0f1f12 !important; margin: 0 0 .35rem 0 !important;
}
div[data-testid="stColumns"] [data-testid="stButton"] > button p:nth-child(3) {
  font-size: .81rem !important; color: #637066 !important;
  font-weight: 400 !important; line-height: 1.5 !important; margin: 0 !important;
}

/* ══ Dataset chips (home strip) ══════════════════════════════════════════════ */
.ds-strip-chip {
  display: inline-flex; align-items: center; gap: .35rem;
  background: #fff; border: 1px solid #e2e8e2; border-radius: 8px;
  padding: 5px 11px; margin: .2rem; font-size: .8rem;
  box-shadow: 0 1px 2px rgba(0,0,0,.04);
  transition: border-color .14s, box-shadow .14s;
}
.ds-strip-chip:hover { border-color: #86efac; box-shadow: 0 2px 8px rgba(22,163,74,.1); }

</style>
"""


def inject_css():
    st.markdown(GLOBAL_CSS, unsafe_allow_html=True)


def render_sidebar(active: str = ""):
    username = st.session_state.get("username", "")
    initial = username[0].upper() if username else "?"
    with st.sidebar:
        st.markdown(f"""
        <div class="sb-brand">
          <div class="sb-icon">🌍</div>
          <div>
            <div class="sb-title">PALs Earth</div>
            <div class="sb-subtitle">Geospatial Platform</div>
          </div>
        </div>
        <div class="sb-user">
          <div class="sb-avatar">{initial}</div>
          <div>
            <div class="sb-uname">{username}</div>
            <div class="sb-urole">Researcher</div>
          </div>
        </div>
        <span class="sb-section-label">Navigation</span>
        """, unsafe_allow_html=True)

        st.page_link("app.py",             label="Home",             icon="🏠")
        st.page_link("pages/1_Extract.py", label="Extract Data",     icon="🗺️")
        st.page_link("pages/2_My_Jobs.py", label="My Jobs",          icon="📋")
        st.page_link("pages/3_Help.py",    label="Dataset Reference", icon="📦")

        st.markdown('<hr class="sb-divider">', unsafe_allow_html=True)
        if st.button("Sign out", use_container_width=True, key="sb_signout"):
            from core.auth import revoke_session_token, clear_session_cookie, COOKIE_NAME
            revoke_session_token(st.query_params.get("t"))
            revoke_session_token(st.context.cookies.get(COOKIE_NAME))
            clear_session_cookie()
            st.session_state["username"] = ""
            st.query_params.clear()
            st.switch_page("app.py")


def render_chat_fab():
    st.markdown("""
    <div class="shmron-fab-wrap">
      <div class="shmron-fab-tip">Ask Shmron AI</div>
      <a href="Extract" class="shmron-fab" title="Shmron AI">💬</a>
    </div>
    """, unsafe_allow_html=True)
