import streamlit as st
import sys
import os
import base64

sys.path.insert(0, '/home/USER/PRECISE/palsearth')

_LOGO_PATH = '/home/USER/PRECISE/palsearth/assets/logo.png'

def _logo_b64() -> str:
    try:
        with open(_LOGO_PATH, 'rb') as f:
            return base64.b64encode(f.read()).decode()
    except Exception:
        return ""

st.set_page_config(
    page_title="PALs Earth",
    page_icon="🌍",
    layout="wide",
    initial_sidebar_state="expanded",
)

from core.ui import inject_css, render_sidebar, render_chat_fab
inject_css()


def show_auth():
    st.markdown("<style>[data-testid='stSidebar']{display:none}</style>",
                unsafe_allow_html=True)

    # Warm ivory background — clean, easy on the eyes
    # Card is applied to the center column's content wrapper only
    st.markdown("""
    <style>
    .stApp { background: #f5f0ea !important; }
    .main .block-container { padding-top: 3.5rem !important; }

    /* White card scoped to the center column only */
    [data-testid="stColumn"]:nth-child(2) > [data-testid="stVerticalBlock"] {
      background: #ffffff;
      border-radius: 16px;
      padding: 2.5rem 2.25rem 2rem;
      box-shadow: 0 6px 32px rgba(0,0,0,.11), 0 1px 4px rgba(0,0,0,.06);
    }

    /* Accent colour bar at top of card */
    [data-testid="stColumn"]:nth-child(2) > [data-testid="stVerticalBlock"]::before {
      content: '';
      display: block;
      height: 5px;
      margin: -2.5rem -2.25rem 2rem;
      border-radius: 16px 16px 0 0;
      background: linear-gradient(90deg, #0c2218 0%, #16a34a 50%, #4ade80 100%);
    }

    /* Tabs: underline style, fully readable */
    [data-baseweb="tab-list"] {
      border-bottom: 2px solid #e8e4de !important;
      gap: 0 !important;
      margin-bottom: .25rem !important;
    }
    [data-baseweb="tab"] {
      flex: 1 !important;
      justify-content: center !important;
      border-radius: 0 !important;
      padding: .65rem 1rem !important;
      font-size: .9rem !important;
      font-weight: 600 !important;
      color: #6b7280 !important;
    }
    [data-baseweb="tab"][aria-selected="true"] { color: #16a34a !important; }
    [data-baseweb="tab-highlight"] { background: #16a34a !important; }
    </style>
    """, unsafe_allow_html=True)

    logo_b64 = _logo_b64()
    logo_img = (
        f'<img src="data:image/png;base64,{logo_b64}" '
        f'style="width:90px;height:auto;display:block;margin:0 auto .9rem;">'
        if logo_b64 else ''
    )

    col_l, col_c, col_r = st.columns([1, 1.5, 1])
    with col_c:
        st.markdown(f"""
        {logo_img}
        <div style="font-family:'Playfair Display',Georgia,serif;
                    font-size:1.85rem;font-weight:700;color:#1a1a1a;
                    text-align:center;letter-spacing:-.01em;margin-bottom:.2rem;">
          PALs Earth
        </div>
        <p style="font-size:.86rem;color:#6b7280;text-align:center;
                  margin-bottom:1.5rem;font-family:'Source Sans 3',sans-serif;">
          Geospatial Data Extraction
        </p>
        """, unsafe_allow_html=True)

        reset_token = st.query_params.get("reset_token")
        if reset_token:
            st.markdown("**Set a new password**")
            with st.form("reset_form", clear_on_submit=False):
                np1 = st.text_input("New password", type="password",
                                    placeholder="minimum 8 characters", key="rst_p1")
                np2 = st.text_input("Confirm new password", type="password",
                                    placeholder="repeat password", key="rst_p2")
                reset_go = st.form_submit_button("Set password", type="primary",
                                                 use_container_width=True)
            if reset_go:
                if np1 != np2:
                    st.error("Passwords do not match.")
                else:
                    from core.auth import reset_password
                    ok, msg = reset_password(reset_token, np1)
                    if ok:
                        st.success(msg)
                        st.query_params.clear()
                        if st.button("Back to sign in"):
                            st.rerun()
                    else:
                        st.error(msg)
            st.markdown("""
            <p style="text-align:center;font-size:.74rem;color:#9ca3af;margin-top:1.25rem;">
              © PALS Lab ·
              <a href="https://placealert.org" style="color:#16a34a;text-decoration:none;">
                placealert.org
              </a>
            </p>
            """, unsafe_allow_html=True)
            return

        tab_in, tab_reg = st.tabs(["  Sign in  ", "  Create account  "])

        with tab_in:
            st.markdown(" ")
            with st.form("login_form", clear_on_submit=False):
                username = st.text_input("Username", placeholder="your_username")
                password = st.text_input("Password", type="password",
                                         placeholder="••••••••")
                submitted = st.form_submit_button("Sign in →", type="primary",
                                                  use_container_width=True)

            if submitted:
                if not username or not password:
                    st.error("Enter both username and password.")
                else:
                    from core.auth import login, create_session_token, set_session_cookie
                    ok, msg = login(username, password)
                    if ok:
                        st.session_state["username"] = username
                        token = create_session_token(username)
                        st.query_params["t"] = token
                        set_session_cookie(token)
                        st.rerun()
                    else:
                        st.error(msg)

            with st.expander("Forgot password?"):
                with st.form("forgot_form", clear_on_submit=True):
                    fp_email = st.text_input("Email address", placeholder="you@institution.ac.uk",
                                             key="fp_email")
                    fp_go = st.form_submit_button("Send reset link", use_container_width=True)
                if fp_go:
                    if not fp_email:
                        st.error("Enter your email address.")
                    else:
                        from core.auth import request_password_reset
                        ok, msg = request_password_reset(fp_email)
                        st.success(msg)

        with tab_reg:
            st.markdown(" ")
            with st.form("reg_form", clear_on_submit=True):
                nu  = st.text_input("Username", placeholder="choose_a_username",     key="ru")
                ne  = st.text_input("Email",    placeholder="you@institution.ac.uk",  key="re")
                np1 = st.text_input("Password", type="password",
                                    placeholder="minimum 8 characters",              key="rp1")
                np2 = st.text_input("Confirm password", type="password",
                                    placeholder="repeat password",                   key="rp2")
                reg_go = st.form_submit_button("Create account", type="primary",
                                               use_container_width=True)

            if reg_go:
                if not all([nu, ne, np1, np2]):
                    st.error("All fields are required.")
                elif np1 != np2:
                    st.error("Passwords do not match.")
                elif len(np1) < 8:
                    st.error("Password must be at least 8 characters.")
                else:
                    from core.auth import register
                    ok, msg = register(nu, np1, ne)
                    if ok:
                        st.markdown("""
                        <div class="pe-success-block" style="margin-top:.5rem;">
                          ✓ Account created — pending admin approval.
                          You will be notified by email once approved.
                        </div>
                        """, unsafe_allow_html=True)
                    else:
                        st.error(msg)

        st.markdown("""
        <p style="text-align:center;font-size:.74rem;color:#9ca3af;margin-top:1.25rem;">
          © PALS Lab ·
          <a href="https://placealert.org" style="color:#16a34a;text-decoration:none;">
            placealert.org
          </a>
        </p>
        """, unsafe_allow_html=True)


def show_home():
    render_sidebar(active="home")
    username = st.session_state.get("username", "")

    # Hero
    st.markdown(f"""
    <div class="pe-hero">
      <div class="pe-hero-eyebrow">PALS Lab · Geospatial Platform</div>
      <div class="pe-hero-title">
        <span>🌍</span> PALs Earth
      </div>
      <p class="pe-hero-sub">
        Extract geospatial indicators from Google Earth Engine for your research
        locations — NDVI, rainfall, temperature, soil properties, elevation and more.
      </p>
    </div>
    """, unsafe_allow_html=True)

    c1, c2, c3 = st.columns(3, gap="medium")
    with c1:
        if st.button(
            "🗺️\n\nExtract Data\n\nUpload locations, then fill in the form yourself or ask Shmron AI to do it for you.",
            key="nav_extract", use_container_width=True
        ):
            st.switch_page("pages/1_Extract.py")

    with c2:
        if st.button(
            "📋\n\nMy Jobs\n\nTrack extraction progress in real time and download results.",
            key="nav_jobs", use_container_width=True
        ):
            st.switch_page("pages/2_My_Jobs.py")

    with c3:
        if st.button(
            "📦\n\nDataset Reference\n\nBrowse everything available — descriptions, Earth Engine collections, units.",
            key="nav_help", use_container_width=True
        ):
            st.switch_page("pages/3_Help.py")

    st.markdown('<hr class="pe-hr">', unsafe_allow_html=True)

    st.markdown("""
    <p style="font-size:.72rem;font-weight:600;color:#637066;
              text-transform:uppercase;letter-spacing:.08em;margin-bottom:.6rem;">
      Supported Datasets
    </p>
    """, unsafe_allow_html=True)

    from core.design import dataset_tile

    datasets_meta = [
        ("NDVI (MODIS)",                     "Vegetation",     "MODIS NDVI",            "Timeseries", "pe-chip-blue"),
        ("Precipitation - CHIRPS",           "Climate",        "CHIRPS Rainfall",       "Timeseries", "pe-chip-blue"),
        ("Temperature - ERA5-Land",          "Climate",        "ERA5 Temperature",      "Timeseries", "pe-chip-blue"),
        ("Temperature + Wet Bulb - MERRA-2", "Climate",        "MERRA-2 Wet Bulb",      "Timeseries", "pe-chip-blue"),
        ("Air Quality - CAMS NRT",           "Air Quality",    "CAMS Air Quality",      "Timeseries", "pe-chip-blue"),
        ("Elevation - MERIT DEM",            "Terrain",        "MERIT Elevation",       "Static",     "pe-chip-purple"),
        ("Soil Nitrogen (iSDAsoil)",         "Soil",           "iSDAsoil N/P/K/Ca",     "Static",     "pe-chip-purple"),
        ("Urban Settlement - GHSL",          "Urban",          "GHSL Urban Settlement", "Static",     "pe-chip-purple"),
        ("Relative Wealth Index (Facebook)", "Socioeconomic",  "Wealth Index (Meta)",   "Static",     "pe-chip-purple"),
    ]
    chips_html = ""
    for ds_name, cat, label, badge, chip_cls in datasets_meta:
        code, color = dataset_tile(ds_name, cat)
        chips_html += (
            f'<span class="ds-strip-chip">'
            f'<span class="pe-ds-tile" style="background:{color};min-width:20px;height:20px;'
            f'font-size:.58rem;padding:0 4px;margin-right:4px;vertical-align:middle;">{code}</span>'
            f'{label} '
            f'<span class="pe-chip {chip_cls}">{badge}</span></span>'
        )
    st.markdown(f'<div style="line-height:2.4;">{chips_html}</div>',
                unsafe_allow_html=True)

    render_chat_fab()


from core.auth import restore_session, set_session_cookie
restore_session()

# Safety net: re-affirm the cookie whenever the ?t= token is still in the URL
# (e.g. right after login, before it's dropped on the next page_link click).
# Idempotent — harmless to call repeatedly with the same token.
if st.session_state.get("username") and st.query_params.get("t"):
    set_session_cookie(st.query_params["t"])

if "username" not in st.session_state or not st.session_state["username"]:
    show_auth()
else:
    show_home()
