"""
PALs Earth — Flask front end.

Replaces the Streamlit app while reusing its entire backend unchanged:
core.auth, core.jobs_db, core.uploads, core.worker, core.extractor,
core.datasets, core.windows and core.ai_agent all come from
/home/USER/PRECISE/palsearth. Nothing about how an extraction actually
runs changes here — only how it's driven and drawn.

Why move at all: Streamlit re-runs the whole script on every interaction and
keeps state on the websocket, which is what caused the sign-out-on-refresh
bug, the "Run this extraction" button appearing a turn late, and the CSS
fights with its own generated markup. Flask renders a page per URL, so those
failure modes stop existing rather than getting patched.
"""
import html
import io
import zipfile
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

PALSEARTH_ROOT = os.environ.get("PALSEARTH_ROOT", "/home/USER/PRECISE/palsearth")
if PALSEARTH_ROOT not in sys.path:
    sys.path.insert(0, PALSEARTH_ROOT)

from flask import (Blueprint, Flask, abort, g, redirect, render_template,
                   request, send_file, url_for, jsonify)

from core import auth
from core import jobs_db
from core.datasets import DATASETS, ghsl_for_year
from core.uploads import stage_uploaded_file
from core.windows import PREGNANCY_WINDOWS
from core.worker import run_job_in_background, cancel_job as _cancel_worker_job
import core.ai_agent as ai_agent
from core.notifications import notify_access_request

import catalog
import state_store
import cohort_query
import cohort_delivery
from core.cohort_catalog import get_cohort, list_cohorts

# Emails allowed to manage Cohort Exposures access — same real admins who
# already administer everything else in this stack (JupyterHub, the unified
# duckrequest dashboard). There's no pre-existing admin flag on PALs Earth
# accounts (the `role` column is always 'user', unused for gating), so this
# introduces the first one, scoped to this one feature.
COHORT_ADMIN_EMAILS = {'admin@example.org', 'admin@example.org'}

COHORT_DATASET_CATEGORIES = {}
for _ds_name, _ds_info in DATASETS.items():
    COHORT_DATASET_CATEGORIES.setdefault(_ds_info.get('category', 'Other'), []).append(_ds_name)

JOBS_DIR = os.path.join(PALSEARTH_ROOT, "jobs")
GHSL_NAME = "Urban Settlement - GHSL"
# (value sent to the extractor, label shown to people)
STAT_OPTIONS = [("mean", "Average"), ("min", "Lowest"), ("max", "Highest"),
                ("sum", "Total"), ("p50", "Median"), ("p95", "95th percentile")]
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

# The URL prefix nginx proxies to us. Defaults to the real one; set
# PALSEARTH_PREFIX=/palsearth-flask to run this alongside the live Streamlit
# app for testing, then drop the override at cutover.
PREFIX = os.environ.get("PALSEARTH_PREFIX", "/palsearth").rstrip("/")

pe = Blueprint("pe", __name__, url_prefix=PREFIX)


# ══════════════════════════════════════════════════════════════════════
# auth plumbing
# ══════════════════════════════════════════════════════════════════════

def _current_user():
    token = request.cookies.get(auth.COOKIE_NAME)
    if not token:
        return None, None
    username = auth.validate_session_token(token)
    if not username:
        return None, None
    return username, token


def login_required(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*a, **kw):
        username, token = _current_user()
        if not username:
            return redirect(url_for("pe.auth", mode="signin"))
        g.username = username
        g.token = token
        row = auth.get_user(username)
        g.role = (row["role"] if row else "user") or "user"
        g.state = state_store.load(username)
        g.state["username"] = username          # core.ai_agent reads this
        ai_agent.set_state_provider(lambda: g.state)
        return fn(*a, **kw)

    return wrapper


def cohort_access_required(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*a, **kw):
        if not auth.get_cohort_access(g.username):
            notify("You don't have access to Cohort Exposures yet. Ask an admin.", "error")
            return redirect(url_for("pe.home"))
        return fn(*a, **kw)

    return wrapper


def cohort_admin_required(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*a, **kw):
        row = auth.get_user(g.username)
        if not row or (row["email"] or "").strip().lower() not in COHORT_ADMIN_EMAILS:
            notify("Admin access required.", "error")
            return redirect(url_for("pe.home"))
        return fn(*a, **kw)

    return wrapper


# ── one-shot messages ───────────────────────────────────────────────────────
# Flask's own flash() keeps them in a cookie, and clearing that cookie depends
# on a "delete" Set-Cookie surviving nginx + Cloudflare. It doesn't here, so a
# shown message came back on every page. Keeping them on the server, against
# the signed-in user, means they clear the moment they're displayed.
def notify(message, category="info"):
    if getattr(g, "state", None) is None:
        return
    g.state.setdefault("_flash", []).append([category, message])
    state_store.save(g.username, g.state)


def _take_notices():
    st = getattr(g, "state", None)
    if not st or not st.get("_flash"):
        return []
    items = st.pop("_flash")
    state_store.save(g.username, st)
    return items



def _save_state():
    if getattr(g, "state", None) is not None:
        state_store.save(g.username, g.state)


# Secure cookies are correct in production (nginx terminates TLS in front of
# us). Set PALSEARTH_COOKIE_SECURE=0 only to exercise the app over plain HTTP
# on the box itself.
COOKIE_SECURE = os.environ.get("PALSEARTH_COOKIE_SECURE", "1") != "0"


def _set_cookie(resp, token):
    resp.set_cookie(
        auth.COOKIE_NAME, token,
        max_age=auth.SESSION_TOKEN_TTL_SECONDS,
        path=PREFIX or "/", samesite="Lax", secure=COOKIE_SECURE, httponly=True,
    )
    return resp


# ══════════════════════════════════════════════════════════════════════
# shared view data
# ══════════════════════════════════════════════════════════════════════

def _running_count(username):
    return sum(1 for j in jobs_db.get_user_jobs(username)
               if j["status"] in ("queued", "running"))


def _nav(active):
    running = _running_count(g.username)
    items = [
        ("home", "Home", "#b8f35a", url_for("pe.home"), None),
        ("extract", "New extraction", "#5fb3f0", url_for("pe.extract"), None),
        ("jobs", "My jobs", "#f0a35f", url_for("pe.jobs"), str(running) if running else None),
        ("datasets", "Datasets", "#c49cf2", url_for("pe.datasets"), None),
    ]
    if auth.get_cohort_access(g.username):
        items.append(("cohort", "Cohort Exposures", "#f25f8c", url_for("pe.cohort_assign"), None))
    return [{"key": k, "label": lbl, "dot": dot, "href": href, "badge": badge,
             "active": k == active} for k, lbl, dot, href, badge in items]


def _shell(active, **extra):
    return dict(nav=_nav(active), username=g.username, role=g.role, **extra)


def _dot_map(points):
    """
    Equal-aspect dot layout for the upload preview (the mockup draws dots on a
    dark grid rather than a real basemap). Longitude is scaled by cos(latitude)
    so shapes aren't stretched; SVG 'meet' then fits it to the panel.
    """
    import math
    pts = [p for p in (points or []) if len(p) == 2]
    if not pts:
        return None
    lat0 = sum(p[0] for p in pts) / len(pts)
    k = math.cos(math.radians(lat0))
    xy = [(p[1] * k, -p[0]) for p in pts]
    xs = [a for a, _ in xy]
    ys = [b for _, b in xy]
    w = (max(xs) - min(xs)) or 0.02
    h = (max(ys) - min(ys)) or 0.02
    pad = max(w, h) * 0.14
    vw, vh = w + 2 * pad, h + 2 * pad
    # keep dots a sensible size relative to the panel, not the data spread
    r = max(vw, vh) * 0.014
    return {
        "view": f"{min(xs) - pad:.5f} {min(ys) - pad:.5f} {vw:.5f} {vh:.5f}",
        "r": f"{r:.5f}",
        "pts": [[round(a, 5), round(b, 5)] for a, b in xy],
    }


def _staged_view():
    """The staged upload, plus the extra presentation fields templates want."""
    s = (g.state or {}).get("staged_upload")
    if not s:
        return None
    pts = s.get("points") or []
    return {
        **s,
        "label": s.get("label") or os.path.basename(s.get("input_filename", "")),
        "n_columns": s.get("n_columns", "—"),
        "n_bad": s.get("n_bad", 0),
        "columns": s.get("columns") or [],
        "date_columns": s.get("date_columns") or [],
        "points_json": json.dumps(pts),
        "dots": _dot_map(pts),
    }


# ══════════════════════════════════════════════════════════════════════
# auth routes
# ══════════════════════════════════════════════════════════════════════

_AUTH_COPY = {
    "signin": ("Welcome back", "Sign in to pick up where you left off.", "Sign in"),
    "signup": ("Start extracting", "Free for researchers. An admin approves new accounts.", "Create account"),
    "forgot": ("Reset your password", "We'll email you a link to set a new one.", "Send reset link"),
    "reset":  ("Set a new password", "Make it at least 8 characters.", "Save password"),
}


def _hero_points():
    """Deterministic pulse dots — same generator the mockup used, fixed seed."""
    seed = [7]

    def rnd():
        seed[0] = (seed[0] * 9301 + 49297) % 233280
        return seed[0] / 233280

    palette = ["#b8f35a", "#7fd3a0", "#5fb3f0"]
    return [{"x": f"{rnd()*95:.1f}%", "y": f"{rnd()*95:.1f}%",
             "s": f"{4 + rnd()*5:.0f}px", "c": palette[int(rnd()*3)],
             "d": f"{rnd()*2.4:.2f}s"} for _ in range(22)]


def _auth_ctx(mode, **kw):
    title, subtitle, cta = _AUTH_COPY[mode]
    actions = {"signin": "pe.do_signin", "signup": "pe.do_signup",
               "forgot": "pe.do_forgot", "reset": "pe.do_reset"}
    return dict(
        mode=mode, title=title, subtitle=subtitle, cta=cta,
        action=url_for(actions[mode]),
        hero_points=_hero_points(),
        preview=catalog.all_views()[:6],
        form={}, error=None, notice=None, reset_token="",
        **kw,
    )


@pe.route("/")
def index():
    token = request.args.get("reset_token")
    if token:
        return redirect(url_for("pe.auth", mode="reset", reset_token=token))
    username, _ = _current_user()
    if username:
        return redirect(url_for("pe.home"))
    return redirect(url_for("pe.auth", mode="signin"))


# Old Streamlit page addresses (bookmarks, emails, the portal's own links).
@pe.route("/Extract")
def legacy_extract():
    return redirect(url_for("pe.extract"))


@pe.route("/My_Jobs")
def legacy_jobs():
    return redirect(url_for("pe.jobs"))


@pe.route("/Help")
def legacy_help():
    return redirect(url_for("pe.datasets"))


@pe.route("/auth/<mode>", endpoint="auth")
def auth_page(mode):
    if mode not in _AUTH_COPY:
        abort(404)
    username, _ = _current_user()
    if username and mode in ("signin", "signup"):
        return redirect(url_for("pe.home"))
    ctx = _auth_ctx(mode)
    if mode == "reset":
        ctx["reset_token"] = request.args.get("reset_token", "")
        if not auth.verify_reset_token(ctx["reset_token"]):
            ctx["error"] = "This reset link is invalid or has expired. Request a new one."
    if request.args.get("registered"):
        ctx["notice"] = ("Account created. An admin has been emailed to approve it. "
                         "You'll be able to sign in once they do.")
    if request.args.get("sent"):
        ctx["notice"] = "If that email is registered and approved, a reset link has been sent."
    if request.args.get("changed"):
        ctx["notice"] = "Password updated. Sign in with your new password."
    return render_template("auth.html", **ctx)


@pe.post("/signin")
def do_signin():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    ok, msg = auth.login(username, password)
    if not ok:
        ctx = _auth_ctx("signin")
        ctx["error"] = msg
        ctx["form"] = {"username": username}
        return render_template("auth.html", **ctx), 401
    token = auth.create_session_token(username)
    resp = redirect(url_for("pe.home"))
    return _set_cookie(resp, token)


@pe.post("/signup")
def do_signup():
    username = (request.form.get("username") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    password2 = request.form.get("password2") or ""

    error = None
    if len(password) < 8:
        error = "Password must be at least 8 characters."
    elif password != password2:
        error = "The two passwords don't match."
    elif not username or not email:
        error = "Username and email are both required."

    if not error:
        ok, msg = auth.register(username, password, email)
        if ok:
            return redirect(url_for("pe.auth", mode="signin", registered=1))
        error = msg

    ctx = _auth_ctx("signup")
    ctx["error"] = error
    ctx["form"] = {"username": username, "email": email}
    return render_template("auth.html", **ctx), 400


@pe.post("/forgot")
def do_forgot():
    auth.request_password_reset(request.form.get("email") or "")
    return redirect(url_for("pe.auth", mode="signin", sent=1))


@pe.post("/reset")
def do_reset():
    token = request.form.get("reset_token") or ""
    password = request.form.get("password") or ""
    password2 = request.form.get("password2") or ""
    if password != password2:
        ctx = _auth_ctx("reset", reset_token=token)
        ctx["error"] = "The two passwords don't match."
        return render_template("auth.html", **ctx), 400
    ok, msg = auth.reset_password(token, password)
    if not ok:
        ctx = _auth_ctx("reset", reset_token=token)
        ctx["error"] = msg
        return render_template("auth.html", **ctx), 400
    return redirect(url_for("pe.auth", mode="signin", changed=1))


@pe.post("/signout")
def signout():
    _, token = _current_user()
    if token:
        auth.revoke_session_token(token)
    resp = redirect(url_for("pe.auth", mode="signin"))
    resp.delete_cookie(auth.COOKIE_NAME, path=PREFIX or "/")
    return resp


# ══════════════════════════════════════════════════════════════════════
# home
# ══════════════════════════════════════════════════════════════════════

@pe.get("/home")
@login_required
def home():
    running = _running_count(g.username)
    hour = datetime.now().hour
    greeting = ("GOOD MORNING" if hour < 12 else
                "GOOD AFTERNOON" if hour < 18 else "GOOD EVENING")
    if running:
        summary = (f"{running} extraction{'s are' if running > 1 else ' is'} running right now. "
                   "Start another, or explore what data is available.")
    else:
        summary = ("All jobs are done. Upload new locations or explore what data "
                   "is available.")

    staged = _staged_view()
    quick = [
        {"num": "↑", "title": "Extract data", "color": "#1f7a4a", "tint": "#e3f5ea",
         "href": url_for("pe.extract"),
         "desc": "Upload locations, then build the request yourself or with Shmron AI."},
        {"num": str(running), "title": "My jobs", "color": "#c2410c", "tint": "#fbe7dc",
         "href": url_for("pe.jobs"),
         "desc": "Watch extractions progress live and download results."},
        {"num": str(len(DATASETS)), "title": "Datasets", "color": "#7c4ddb", "tint": "#efe9fe",
         "href": url_for("pe.datasets"),
         "desc": "Descriptions, Earth Engine collections, units and resolution."},
    ]
    cohort_access = auth.get_cohort_access(g.username)
    # "Has the toggle but no country checked yet" is just as unusable as no
    # access at all, so it gets the same request-access prompt rather than
    # silently dead-ending at the Cohort tab.
    cohort_scoped = cohort_access and any(auth.get_all_cohort_country_access(g.username).values())
    # Only computed for someone who'd actually see the request form -- no
    # point querying every cohort's country list on every home-page view
    # for users who already have working access.
    request_cohorts = [] if cohort_scoped else list_cohorts()
    request_cohort_countries = _per_cohort_country_options(request_cohorts) if request_cohorts else {}
    return render_template("home.html", **_shell(
        "home", greeting=greeting, summary=summary, staged=staged,
        map_points=_normalised_points(staged), quick_cards=quick,
        datasets=catalog.all_views()[:10],
        cohort_access=cohort_access, cohort_scoped=cohort_scoped,
        request_cohorts=request_cohorts, request_cohort_countries=request_cohort_countries,
    ))


@pe.post("/cohort/request-access")
@login_required
def cohort_request_access():
    cohort_key = request.form.get("cohort", "")
    try:
        cohort = get_cohort(cohort_key)
    except KeyError:
        cohort = None

    if cohort:
        cohort_label = cohort["label"]
        scope_col = cohort.get("access_scope_column")
        all_options = cohort_query.distinct_values(cohort_key, scope_col) if scope_col else []
        if not all_options:
            countries_desc = "Whole cohort (no country scoping for this one)"
        elif request.form.get(f"all__{cohort_key}"):
            countries_desc = "All countries"
        else:
            picked = set(request.form.getlist(f"countries__{cohort_key}"))
            valid = [c for c in all_options if c in picked]
            countries_desc = ", ".join(valid) if valid else "none specified"
    else:
        cohort_label = "Not specified"
        countries_desc = "N/A"

    row = auth.get_user(g.username)
    requester_email = (row["email"] if row else "") or ""
    for admin_email in COHORT_ADMIN_EMAILS:
        notify_access_request(admin_email, g.username, requester_email, cohort_label, countries_desc)
    notify("Request sent. An admin will set up your Cohort Exposures access.")
    return redirect(url_for("pe.home"))


def _normalised_points(staged, limit=60):
    """Lat/lon → % offsets inside the stylised hero panel."""
    if not staged:
        return []
    pts = staged.get("points") or []
    if not pts:
        return []
    step = max(1, len(pts) // limit)
    pts = pts[::step][:limit]
    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    lat0, lat1 = min(lats), max(lats)
    lon0, lon1 = min(lons), max(lons)
    dlat = (lat1 - lat0) or 1.0
    dlon = (lon1 - lon0) or 1.0
    out = []
    for lat, lon in pts:
        out.append({
            "x": round(8 + 84 * (lon - lon0) / dlon, 1),
            "y": round(8 + 84 * (1 - (lat - lat0) / dlat), 1),
        })
    return out


# ══════════════════════════════════════════════════════════════════════
# extract — upload
# ══════════════════════════════════════════════════════════════════════

class _Upload(io.BytesIO):
    """core.uploads expects a Streamlit UploadedFile: file-like plus `.name`."""

    def __init__(self, data, name):
        super().__init__(data)
        self.name = name


def _date_columns(df):
    """Columns that hold dates, so per-row date options are only offered when
    the uploaded file can actually support them."""
    import pandas as pd
    out = []
    for c in df.columns:
        col = df[c].dropna()
        if col.empty or pd.api.types.is_numeric_dtype(col):
            continue
        sample = col.astype(str).head(25)
        try:
            parsed = pd.to_datetime(sample, errors="coerce")
        except Exception:
            continue
        if parsed.notna().mean() >= 0.8:
            out.append(str(c))
    return out


def _describe_upload(staged, original_name):
    """Adds preview metadata (columns, bad coords, sample points) for the UI."""
    info = {**staged, "label": original_name, "n_columns": "—", "n_bad": 0,
            "columns": [], "points": []}
    path = staged["input_filename"]
    try:
        if path.endswith(".csv"):
            import pandas as pd
            df = pd.read_csv(path)
            info["columns"] = [str(c) for c in df.columns]
            info["n_columns"] = len(df.columns)
            info["date_columns"] = _date_columns(df)
            lat_c = ["latitude", "lat", "LAT", "Latitude", "LATITUDE", "y", "Y"]
            lon_c = ["longitude", "lon", "LON", "Longitude", "LONGITUDE", "x", "X", "lng", "LNG"]
            lat = next((c for c in lat_c if c in df.columns), None)
            lon = next((c for c in lon_c if c in df.columns), None)
            if lat and lon:
                coords = df[[lat, lon]].apply(pd.to_numeric, errors="coerce")
                good = coords.dropna()
                good = good[(good[lat].between(-90, 90)) & (good[lon].between(-180, 180))]
                info["n_bad"] = int(len(df) - len(good))
                step = max(1, len(good) // 500)
                info["points"] = [[round(float(a), 5), round(float(b), 5)]
                                  for a, b in good.iloc[::step][[lat, lon]].values[:500]]
        else:
            import geopandas as gpd
            gdf = gpd.read_file(path)
            info["columns"] = [str(c) for c in gdf.columns if c != "geometry"]
            info["n_columns"] = len(info["columns"])
            cent = gdf.geometry.representative_point()
            step = max(1, len(cent) // 500)
            info["points"] = [[round(float(p.y), 5), round(float(p.x), 5)]
                              for p in cent.iloc[::step][:500]]
    except Exception as e:      # a preview failing must never block the job
        info["preview_error"] = str(e)
    return info


@pe.post("/upload")
@login_required
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        notify("Choose a CSV or zipped shapefile first.", "error")
        return redirect(url_for("pe.extract"))
    data = f.read()
    if len(data) > MAX_UPLOAD_BYTES:
        notify("That file is over the 200 MB limit.", "error")
        return redirect(url_for("pe.extract"))
    try:
        staged = stage_uploaded_file(_Upload(data, f.filename))
    except ValueError as e:
        notify(str(e), "error")
        return redirect(url_for("pe.extract"))
    except Exception as e:
        notify(f"Could not read that file: {e}", "error")
        return redirect(url_for("pe.extract"))

    g.state["staged_upload"] = _describe_upload(staged, f.filename)
    _save_state()
    notify(f"{staged['n_locations']} locations loaded from {f.filename}.")
    return redirect(url_for("pe.extract", tab=request.form.get("tab") or "ai"))


@pe.post("/unstage")
@login_required
def unstage():
    g.state.pop("staged_upload", None)
    g.state.pop("pending_plan", None)
    _save_state()
    return redirect(url_for("pe.extract"))


# ══════════════════════════════════════════════════════════════════════
# extract — page
# ══════════════════════════════════════════════════════════════════════

@pe.get("/extract")
@login_required
def extract():
    tab = "manual" if request.args.get("tab") == "manual" else "ai"
    staged = _staged_view()
    custom = g.state.get("custom_datasets") or {}
    selected = set(g.state.get("manual_selection") or [])

    steps = [
        {"n": 1, "label": "Upload", "active": True, "done": bool(staged)},
        {"n": 2, "label": "Build", "active": bool(staged), "done": False},
        {"n": 3, "label": "Track", "active": False, "done": False},
    ]
    windows = [{"key": k, "label": v.get("label", k),
                "span": v.get("description", "")}
               for k, v in PREGNANCY_WINDOWS.items()]

    today = datetime.now().date()
    pending_plan = g.state.get("pending_plan")
    manual_pending_plan = pending_plan if pending_plan and pending_plan.get("_source") == "manual" else None
    return render_template("extract.html", **_shell(
        "extract", tab=tab, staged=staged, steps=steps,
        datasets=catalog.all_views(custom), selected=selected,
        stat_options=STAT_OPTIONS, windows=windows,
        ghsl_years=DATASETS.get(GHSL_NAME, {}).get("years"),
        default_start=(today - timedelta(days=730)).isoformat(),
        default_end=today.isoformat(),
        manual_pending_plan=manual_pending_plan,
        suggestions=[{"text": t, "color": c, "tint": ti}
                     for t, c, ti in catalog.SUGGESTIONS],
        transcript=_transcript_view(),
    ))


# ══════════════════════════════════════════════════════════════════════
# extract — Shmron AI
# ══════════════════════════════════════════════════════════════════════

_MD_INLINE = (
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])"), r"<em>\1</em>"),
)


def _md(text):
    """Escape first, then apply the small subset of Markdown the agent emits."""
    out, buf = [], []

    def flush_list():
        if buf:
            out.append("<ul>" + "".join(f"<li>{x}</li>" for x in buf) + "</ul>")
            buf.clear()

    for raw in (text or "").split("\n"):
        line = html.escape(raw.strip())
        for pat, rep in _MD_INLINE:
            line = pat.sub(rep, line)
        if re.match(r"^(\-|\*|•)\s+", line):
            buf.append(re.sub(r"^(\-|\*|•)\s+", "", line))
        elif re.match(r"^\d+\.\s+", line):
            buf.append(re.sub(r"^\d+\.\s+", "", line))
        elif not line:
            flush_list()
        else:
            flush_list()
            out.append(f"<p>{line}</p>")
    flush_list()
    return "".join(out) or "<p></p>"


def _plan_summary(plan):
    bits = []
    mode = plan.get("date_mode")
    if mode == "pregnancy_windows":
        keys = plan.get("window_keys") or []
        bits.append(f"{len(keys)} pregnancy window{'s' if len(keys) != 1 else ''} ({', '.join(keys)})")
    elif mode == "index_window":
        cw = plan.get("custom_window") or {}
        idx_col = (plan.get("ref_date_cols") or {}).get("index", "?")
        bits.append(f"{cw.get('day_from', '?')} to {cw.get('day_to', '?')} days around '{idx_col}'")
    elif mode == "case_crossover":
        cw = plan.get("custom_window") or {}
        idx_col = (plan.get("ref_date_cols") or {}).get("index", "?")
        day_from, day_to = cw.get("day_from"), cw.get("day_to")
        span_txt = f"{day_from} to {day_to} days" if day_from is not None and day_to is not None else "lag span not set"
        try:
            window_days = int(day_to) - int(day_from) + 1
            bits.append(f"case-crossover around '{idx_col}' ({span_txt}, auto referent days), "
                        f"~{window_days} rows/period × ~4-5 periods (1 case + referents) per participant")
        except (TypeError, ValueError):
            bits.append(f"case-crossover around '{idx_col}' ({span_txt}, auto referent days)")
    elif plan.get("start_date"):
        bits.append(f"{plan['start_date']} → {plan.get('end_date', '')}")
    else:
        bits.append("static (no date range needed)")
    if mode in ("pregnancy_windows", "index_window", "case_crossover") and plan.get("output_shape") == "long":
        bits.append("daily/long format")
    stats = plan.get("stats") or []
    if stats:
        bits.append(", ".join(stats))
    bits.append((plan.get("output_format") or "csv").upper())
    return " · ".join(bits)


_SAMPLE_LOCATIONS = [
    {"PID": "P001", "Latitude": "-3.930", "Longitude": "39.586"},
    {"PID": "P002", "Latitude": "-3.858", "Longitude": "39.474"},
]


def _sample_output_preview(plan, custom_datasets):
    """
    Tiny, obviously-fake example tables (wide AND long, where both apply)
    for the currently drafted plan — built from the exact same column-
    naming logic the real extractor uses (core.extractor._col_names /
    _col_names_windowed), so the preview's headers always match what a
    real run would actually produce. Only the cell values are placeholders.
    Lets someone see "one row per person" vs "one row per day, with these
    columns" before spending a real extraction on it, so they can ask for
    a different shape/window instead of finding out after the fact.
    """
    from core.extractor import _col_names, _col_names_windowed, _dataset_stats

    mode = plan.get("date_mode")
    if mode not in ("pregnancy_windows", "index_window", "case_crossover"):
        return None  # plain fixed-range jobs aren't really an AI-drafted case

    all_ds = {**DATASETS, **(custom_datasets or {})}
    names = [n for n in (plan.get("dataset_names") or []) if n in all_ds]
    if not names:
        return None
    stats_requested = plan.get("stats") or ["mean"]
    ds_info_stats = [(all_ds[n], _dataset_stats(all_ds[n], stats_requested)) for n in names]

    def fake(i):
        return round(8 + (i * 37) % 300 / 10, 1)

    preview = {"wide": None, "long": None, "lag_matrix": None}

    if mode == "index_window":
        cw = plan.get("custom_window") or {}
        try:
            day_from, day_to = int(cw.get("day_from")), int(cw.get("day_to"))
        except (TypeError, ValueError):
            day_from, day_to = None, None
        # Capped at a 30-day span just for the preview table's own
        # readability — the real extraction has no such limit.
        if day_from is not None and day_to is not None and 0 <= (day_to - day_from) <= 30:
            cols = ["PID", "Latitude", "Longitude"]
            for ds_info, ds_stats in ds_info_stats:
                base_cols = _col_names(ds_info, ds_stats)
                for offset in range(day_from, day_to + 1):
                    cols += [f"{c}_lag{offset}" for c in base_cols]
            rows = []
            for i, loc in enumerate(_SAMPLE_LOCATIONS):
                row = dict(loc)
                for j, c in enumerate(cols):
                    row.setdefault(c, fake(i * 17 + j))
                rows.append(row)
            preview["lag_matrix"] = {"columns": cols, "rows": rows}

    if mode != "case_crossover":
        window_keys = plan.get("window_keys") or ([] if mode == "pregnancy_windows" else ["custom"])
        cols = ["PID", "Latitude", "Longitude"]
        for ds_info, ds_stats in ds_info_stats:
            for wk in window_keys:
                cols += _col_names_windowed(ds_info, ds_stats, wk)
        if window_keys:
            rows = []
            for i, loc in enumerate(_SAMPLE_LOCATIONS):
                row = dict(loc)
                for j, c in enumerate(cols):
                    row.setdefault(c, fake(i * 13 + j))
                rows.append(row)
            preview["wide"] = {"columns": cols, "rows": rows}

    front = ["Exposure_Date", "Window", "Day_Offset"] + (["Role"] if mode == "case_crossover" else [])
    long_cols = ["PID"] + front
    for ds_info, ds_stats in ds_info_stats:
        long_cols += _col_names(ds_info, ds_stats)
    window_label = ((plan.get("window_keys") or ["T1"])[0] if mode == "pregnancy_windows"
                    else "case" if mode == "case_crossover" else "custom")
    base = datetime(2026, 3, 11).date()
    rows = []
    for d in range(4):
        row = {"PID": "P001", "Exposure_Date": (base + timedelta(days=d)).isoformat(),
               "Window": (window_label if d < 2 or mode != "case_crossover" else "control_1"),
               "Day_Offset": d - 1}
        if mode == "case_crossover":
            row["Role"] = "case" if row["Window"] == "case" else "control"
        for j, c in enumerate(long_cols):
            if c not in row:
                row[c] = fake(d * 11 + j)
        rows.append(row)
    preview["long"] = {"columns": long_cols, "rows": rows}

    return preview


def _transcript_view():
    """Stored transcript → render-ready messages, with the plan card attached."""
    msgs = []
    for m in (g.state.get("transcript") or []):
        events = [{"type": e.get("type", "text"), "html": _md(e.get("content", ""))}
                  for e in m.get("events", [])]
        msgs.append({"role": m.get("role", "ai"), "events": events, "proposal": None})

    plan = g.state.get("pending_plan")
    if plan and msgs and plan.get("_source") != "manual":
        custom = g.state.get("custom_datasets") or {}
        msgs[-1]["proposal"] = {
            "chips": catalog.chips_for(plan.get("dataset_names"), custom),
            "summary": _plan_summary(plan),
            "preview": _sample_output_preview(plan, custom),
            "warnings": plan.get("_stat_warnings"),
        }
    return msgs


def _anthropic_client():
    from core.apikey import get_anthropic_key   # shared with the catalogue's Shmron
    key = get_anthropic_key()
    if not key:
        return None
    import anthropic
    return anthropic.Anthropic(api_key=key)


def _agent_context():
    """What Shmron needs to know about this user's session, given fresh each turn."""
    lines = [f"Today's date: {datetime.now().date().isoformat()}."]
    st = (g.state or {}).get("staged_upload")
    if st:
        kind = "polygons" if st.get("geometry_type") == "polygons" else "points"
        lines.append(
            f"Uploaded file: {st.get('label') or 'uploaded file'}, {st.get('n_locations')} "
            f"{kind}, already validated and ready to use.")
        if st.get("columns"):
            lines.append("Its columns: " + ", ".join(st["columns"][:40]) + ".")
        if st.get("date_columns"):
            lines.append("Columns that hold dates: " + ", ".join(st["date_columns"]) + ".")
        else:
            lines.append("The file has no date columns, so use a calendar date range.")
        if st.get("n_bad"):
            lines.append(f"{st['n_bad']} rows have missing or out-of-range coordinates. "
                         "They are kept in the output with empty values for every "
                         "variable (no exposure can be extracted without a location). "
                         "Mention this once, briefly.")
    else:
        lines.append("No file is uploaded yet. If you need locations, ask the user to "
                     "upload a CSV or zipped shapefile in step 1 above the chat.")
    return "\n".join(lines)


@pe.post("/ask")
@login_required
def ask():
    text = (request.form.get("message") or "").strip()
    if not text:
        return redirect(url_for("pe.extract", tab="ai"))

    client = _anthropic_client()
    if client is None:
        notify("Shmron AI is not configured on this server (no API key set). "
              "Use the Manual tab meanwhile.", "error")
        return redirect(url_for("pe.extract", tab="ai"))

    transcript = g.state.setdefault("transcript", [])
    transcript.append({"role": "user", "events": [{"type": "text", "content": text}]})

    messages = g.state.setdefault("ai_messages", [])
    messages.append({"role": "user", "content": text})

    # The plan card must survive a turn where the AI doesn't re-draft (e.g. the
    # user says "thanks" or repeats themselves). It's replaced when a new plan
    # is drafted, and cleared on Run, Replace file, or Clear conversation.
    try:
        events = ai_agent.run_agent_turn(client, messages, context=_agent_context())
    except Exception as e:
        events = [{"type": "text",
                   "content": f"I couldn't reach the model just now: {e}"}]
    transcript.append({"role": "ai", "events": events})
    _save_state()
    return redirect(url_for("pe.extract", tab="ai"))


@pe.post("/clear-chat")
@login_required
def clear_chat():
    g.state = state_store.clear_chat(g.username)
    g.state["username"] = g.username
    return redirect(url_for("pe.extract", tab="ai"))


@pe.post("/run-plan")
@login_required
def run_plan():
    plan = g.state.get("pending_plan")
    source = plan.get("_source", "shmron_ai") if plan else "shmron_ai"
    home_tab = "manual" if source == "manual" else "ai"
    if not plan:
        notify("That plan is no longer available. Ask Shmron AI again." if home_tab == "ai"
               else "That plan is no longer available. Pick your options and submit again.", "error")
        return redirect(url_for("pe.extract", tab=home_tab))

    # pregnancy_windows/index_window genuinely offer a wide-vs-long choice —
    # require it to have been explicitly confirmed via the preview cards
    # rather than silently defaulting, so a stale/JS-bypassed submit can't
    # slip through with whatever shape happened to be in the drafted plan.
    # A manual-sourced plan already locked in its output_shape back in
    # submit_manual (its own form has no shape-preview-card step to repeat
    # here), so this re-confirmation is Shmron-AI-only.
    if source != "manual" and plan.get("date_mode") in ("pregnancy_windows", "index_window"):
        chosen_shape = request.form.get("output_shape")
        valid_shapes = ("wide", "long", "lag_matrix") if plan.get("date_mode") == "index_window" else ("wide", "long")
        if chosen_shape not in valid_shapes:
            notify("Pick an output format from the preview before running.", "error")
            return redirect(url_for("pe.extract", tab=home_tab))
        plan = dict(plan, output_shape=chosen_shape)

    # A manual-sourced plan may carry a GHSL year override that submit_manual
    # computed before staging the plan (deferred past the stat-feasibility
    # warning) — apply/restore it exactly like submit_manual's own direct
    # path does, so a deferred "Run anyway" doesn't silently lose it.
    ghsl_override = plan.get("_ghsl_override")
    customs = g.state.setdefault("custom_datasets", {})
    had_ghsl = GHSL_NAME in customs
    previous_ghsl = customs.get(GHSL_NAME)
    if ghsl_override:
        customs[GHSL_NAME] = ghsl_override
    try:
        result = ai_agent.execute_plan(plan, source=source)
    finally:
        if ghsl_override:
            if had_ghsl:
                customs[GHSL_NAME] = previous_ghsl
            else:
                customs.pop(GHSL_NAME, None)

    if not result.get("ok"):
        notify(result.get("message") or "Could not start that extraction.", "error")
        _save_state()
        return redirect(url_for("pe.extract", tab=home_tab))
    g.state.pop("pending_plan", None)
    _save_state()
    notify("Extraction submitted.")
    return redirect(url_for("pe.jobs"))


# ══════════════════════════════════════════════════════════════════════
# extract — manual submit
# ══════════════════════════════════════════════════════════════════════

@pe.post("/submit-manual")
@login_required
def submit_manual():
    names = request.form.getlist("datasets")
    stats = request.form.getlist("stats") or ["mean"]
    g.state["manual_selection"] = names

    if not names:
        notify("Pick at least one dataset.", "error")
        _save_state()
        return redirect(url_for("pe.extract", tab="manual"))

    plan = {
        "dataset_names": names,
        "stats": stats,
        "output_format": request.form.get("output_format") or "csv",
        "date_mode": request.form.get("date_mode") or "range",
        "temp_unit": request.form.get("temp_unit") or "kelvin",
    }
    if plan["date_mode"] == "pregnancy_windows":
        plan["window_keys"] = request.form.getlist("window_keys")
        plan["ref_date_cols"] = {
            "conception": (request.form.get("conception_col") or "").strip(),
            "delivery": (request.form.get("delivery_col") or "").strip() or None,
        }
        plan["output_shape"] = request.form.get("output_shape") or "wide"
    elif plan["date_mode"] == "index_window":
        plan["ref_date_cols"] = {"index": (request.form.get("index_col") or "").strip()}
        try:
            plan["custom_window"] = {
                "day_from": int(request.form.get("day_from") or 0),
                "day_to": int(request.form.get("day_to") or 0),
            }
        except ValueError:
            notify("Days-before/days-after must be whole numbers.", "error")
            _save_state()
            return redirect(url_for("pe.extract", tab="manual"))
        plan["output_shape"] = request.form.get("output_shape_idx") or "wide"
    elif plan["date_mode"] == "case_crossover":
        plan["ref_date_cols"] = {"index": (request.form.get("index_col_cc") or "").strip()}
        try:
            plan["custom_window"] = {
                "day_from": int(request.form.get("day_from_cc") or 0),
                "day_to": int(request.form.get("day_to_cc") or 0),
            }
        except ValueError:
            notify("Days-before/days-after must be whole numbers.", "error")
            _save_state()
            return redirect(url_for("pe.extract", tab="manual"))
        plan["output_shape"] = "long"
    else:
        plan["start_date"] = request.form.get("start_date") or None
        plan["end_date"] = request.form.get("end_date") or None

    # GHSL epoch override, resolved up front so it's available either way
    # (run now, or carried along if the stat-feasibility check below defers
    # to a confirmation step).
    year = request.form.get("ghsl_year")
    ghsl_override = None
    if GHSL_NAME in names and year:
        try:
            ghsl_override = ghsl_for_year(int(year))
        except (ValueError, KeyError) as e:
            notify(f"GHSL year problem: {e}", "error")
            _save_state()
            return redirect(url_for("pe.extract", tab="manual"))

    # Stat-feasibility: warn (never block) when the requested stats can't
    # come back as genuinely distinct numbers given how these rows will be
    # shaped — e.g. mean/min/max for Precipitation - CHIRPS on single-day
    # rows, when CHIRPS only has one (total) value per day. The user still
    # gets to run exactly what they asked for; this just makes sure they've
    # seen why those columns will match before they do, rather than finding
    # out from a confusing CSV afterward. See core.ai_agent.
    # check_stat_feasibility.
    all_ds = {**DATASETS, **(g.state.get("custom_datasets") or {}),
              **({GHSL_NAME: ghsl_override} if ghsl_override else {})}
    warnings = ai_agent.check_stat_feasibility(names, plan["stats"], plan.get("date_mode", "range"),
                                                plan.get("output_shape"), all_ds)
    if warnings and not request.form.get("confirm_stat_warning"):
        plan["_source"] = "manual"
        plan["_stat_warnings"] = warnings
        plan["_ghsl_override"] = ghsl_override
        g.state["pending_plan"] = plan
        _save_state()
        notify("Before this runs, see the note below, then click \"Run anyway\" if this is what you want.",
               "warn")
        return redirect(url_for("pe.extract", tab="manual"))

    customs = g.state.setdefault("custom_datasets", {})
    had = GHSL_NAME in customs
    previous = customs.get(GHSL_NAME)
    if ghsl_override:
        customs[GHSL_NAME] = ghsl_override
    try:
        result = ai_agent.execute_plan(plan, source="manual")
    finally:
        if ghsl_override:
            if had:
                customs[GHSL_NAME] = previous
            else:
                customs.pop(GHSL_NAME, None)

    if not result.get("ok"):
        notify(result.get("message") or "Could not start that extraction.", "error")
        _save_state()
        return redirect(url_for("pe.extract", tab="manual"))

    g.state.pop("pending_plan", None)
    _save_state()
    notify("Extraction submitted.")
    return redirect(url_for("pe.jobs"))


# ══════════════════════════════════════════════════════════════════════
# jobs
# ══════════════════════════════════════════════════════════════════════

_PILL = {
    "queued":   ("#f0ede4", "#5d6b62", "Queued"),
    "running":  ("#e2eefb", "#2b7fd6", "Running"),
    "complete": ("#e3f5ea", "#1f7a4a", "Complete"),
    "failed":   ("#fdecea", "#b42318", "Failed"),
}


def _when(iso):
    try:
        dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).astimezone()
    except Exception:
        return iso or ""
    today = datetime.now().astimezone().date()
    if dt.date() == today:
        return f"Today {dt:%H:%M}"
    if dt.date() == today - timedelta(days=1):
        return f"Yesterday {dt:%H:%M}"
    return f"{dt:%d %b %H:%M}"


def _count_locations(path):
    """Rows in a CSV input (cheap line count); None when it can't be known."""
    try:
        if path and path.endswith(".csv"):
            with open(path, "rb") as f:
                return max(sum(1 for _ in f) - 1, 0)
    except OSError:
        pass
    return None


def _job_view(row):
    status = row["status"]
    bg, fg, label = _PILL.get(status, _PILL["queued"])
    try:
        names = json.loads(row["datasets"])
    except Exception:
        names = []
    try:
        custom = json.loads(row["custom_datasets"] or "{}")
    except Exception:
        custom = {}
    chips = catalog.chips_for(names, custom)

    if row["date_mode"] == "pregnancy_windows":
        try:
            keys = json.loads(row["window_keys"] or "[]")
        except Exception:
            keys = []
        span = f"{len(keys)} window{'s' if len(keys) != 1 else ''}"
    elif row["date_mode"] in ("index_window", "case_crossover"):
        try:
            cw = json.loads(row["custom_window"] or "{}")
        except Exception:
            cw = {}
        # Was `label = ...` here, which clobbered the status-pill label
        # (Queued/Running/Complete/Failed) computed above with this static
        # date-mode text instead — since that text never changes across a
        # job's lifecycle, the live-poll JS (which reloads the page only
        # when status_label changes) could never detect completion, so the
        # page silently stopped auto-refreshing for any index_window/
        # case_crossover job until a manual reload.
        window_label = "case-crossover" if row["date_mode"] == "case_crossover" else "custom window"
        span = f"{window_label} ({cw.get('day_from', '?')}→{cw.get('day_to', '?')}d)"
    elif row["start_date"]:
        span = f"{row['start_date']} → {row['end_date']}"
    else:
        span = "static"

    n_loc = _count_locations(row["input_filename"])
    shorts = " + ".join(c["short"] for c in chips[:3]) or "extraction"
    prefix = "Shmron AI" if row["source"] == "shmron_ai" else "Manual"
    return {
        "id": row["id"],
        "short_id": row["id"][:8].upper(),
        "title": f"{prefix}: {shorts}" + ("…" if len(chips) > 3 else ""),
        "status": status,
        "status_label": label,
        "pill_bg": bg,
        "pill_fg": fg,
        "is_running": status in ("queued", "running"),
        "is_complete": status == "complete",
        "is_failed": status == "failed",
        "progress": int(row["progress"] or 0),
        "chips": chips,
        "meta": " · ".join(filter(None, [
            (f"{n_loc:,} {row['geometry_type']}" if n_loc else row["geometry_type"]),
            span, (row["output_format"] or "csv").upper()])),
        "fmt": (row["output_format"] or "csv").upper(),
        "created": _when(row["created_at"]),
        "error_msg": row["error_msg"],
        "output_filename": row["output_filename"],
        "source": row["source"],
    }


@pe.get("/jobs")
@login_required
def jobs():
    views = [_job_view(r) for r in jobs_db.get_user_jobs(g.username)]
    counts = {
        "total": len(views),
        "running": sum(1 for v in views if v["is_running"]),
        "complete": sum(1 for v in views if v["is_complete"]),
        "failed": sum(1 for v in views if v["is_failed"]),
    }
    stats = [
        {"key": "total", "label": "Total", "value": counts["total"], "color": "#10231a", "tint": "#ebe7dc"},
        {"key": "running", "label": "Running", "value": counts["running"], "color": "#2b7fd6", "tint": "#e2eefb"},
        {"key": "complete", "label": "Complete", "value": counts["complete"], "color": "#1f7a4a", "tint": "#e3f5ea"},
        {"key": "failed", "label": "Failed", "value": counts["failed"], "color": "#b42318", "tint": "#fdecea"},
    ]
    return render_template("jobs.html", **_shell(
        "jobs", jobs=views, job_stats=stats, has_active=counts["running"] > 0,
    ))


@pe.get("/jobs.json")
@login_required
def jobs_json():
    views = [_job_view(r) for r in jobs_db.get_user_jobs(g.username)]
    stats = [
        {"key": "total", "value": len(views)},
        {"key": "running", "value": sum(1 for v in views if v["is_running"])},
        {"key": "complete", "value": sum(1 for v in views if v["is_complete"])},
        {"key": "failed", "value": sum(1 for v in views if v["is_failed"])},
    ]
    slim = [{k: v[k] for k in ("id", "progress", "status", "status_label",
                               "pill_bg", "pill_fg", "is_running")} for v in views]
    return jsonify({"jobs": slim, "stats": stats})


def _owned_job(job_id):
    row = jobs_db.get_job(job_id)
    if not row or row["username"] != g.username:
        abort(404)
    return row


@pe.get("/jobs/<job_id>/download")
@login_required
def download(job_id):
    row = _owned_job(job_id)
    name = row["output_filename"]
    if not name or os.path.basename(name) != name:
        abort(404)
    path = os.path.join(JOBS_DIR, name)
    meta = request.args.get("meta")
    if meta:
        stem = path.rsplit(".", 1)[0] if "." in os.path.basename(path) else path
        path = f"{stem}_metadata.{'pdf' if meta == 'pdf' else 'txt'}"
    if not os.path.isfile(path):
        notify("That file is no longer on the server.", "error")
        return redirect(url_for("pe.jobs"))
    if not row["downloaded"]:
        jobs_db.update_job(job_id, downloaded=1)
    return send_file(path, as_attachment=True,
                     download_name=os.path.basename(path))


@pe.get("/jobs/<job_id>/download-zip")
@login_required
def download_zip(job_id):
    row = _owned_job(job_id)
    name = row["output_filename"]
    if not name or os.path.basename(name) != name:
        abort(404)
    data_path = os.path.join(JOBS_DIR, name)
    if not os.path.isfile(data_path):
        notify("That file is no longer on the server.", "error")
        return redirect(url_for("pe.jobs"))
    stem = data_path.rsplit(".", 1)[0] if "." in os.path.basename(data_path) else data_path

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(data_path, arcname=os.path.basename(data_path))
        for ext in ("txt", "pdf"):
            meta_path = f"{stem}_metadata.{ext}"
            if os.path.isfile(meta_path):
                zf.write(meta_path, arcname=os.path.basename(meta_path))
    buf.seek(0)

    if not row["downloaded"]:
        jobs_db.update_job(job_id, downloaded=1)
    return send_file(buf, as_attachment=True,
                     download_name=os.path.basename(stem) + "_package.zip",
                     mimetype="application/zip")


@pe.post("/jobs/<job_id>/cancel")
@login_required
def cancel(job_id):
    row = _owned_job(job_id)
    if row["status"] not in ("queued", "running"):
        notify("That job already finished.", "error")
        return redirect(url_for("pe.jobs"))
    _cancel_worker_job(job_id)
    # The worker may have finished between the click and the kill signal —
    # only mark it cancelled if it's still queued/running right now.
    fresh = jobs_db.get_job(job_id)
    if fresh and fresh["status"] in ("queued", "running"):
        jobs_db.update_job(job_id, status="failed", error_msg="Cancelled by user")
    notify("Job cancelled.")
    return redirect(url_for("pe.jobs"))


@pe.post("/jobs/<job_id>/retry")
@login_required
def retry(job_id):
    _owned_job(job_id)
    jobs_db.update_job(job_id, status="queued", progress=0, error_msg=None)
    run_job_in_background(job_id)
    notify("Job requeued. It's running again.")
    return redirect(url_for("pe.jobs"))


@pe.post("/jobs/<job_id>/delete")
@login_required
def delete_job(job_id):
    row = _owned_job(job_id)
    if row["status"] in ("queued", "running"):
        notify("That job is still running. Wait for it to finish first.", "error")
        return redirect(url_for("pe.jobs"))
    name = row["output_filename"]
    if name and os.path.basename(name) == name:
        for p in (os.path.join(JOBS_DIR, name),
                  os.path.join(JOBS_DIR, name.rsplit(".", 1)[0] + "_metadata.txt")):
            try:
                os.remove(p)
            except OSError:
                pass
    jobs_db.delete_job(job_id)
    notify("Job deleted.")
    return redirect(url_for("pe.jobs"))


# ══════════════════════════════════════════════════════════════════════
# datasets
# ══════════════════════════════════════════════════════════════════════

@pe.get("/datasets")
@login_required
def datasets():
    group = request.args.get("grp") or "All"
    q = (request.args.get("q") or "").strip()
    ql = q.lower()
    views = catalog.all_views(g.state.get("custom_datasets"))
    if group != "All":
        views = [d for d in views if d["grp"] == group]
    if ql:
        views = [d for d in views if ql in d["search"]]
    return render_template("datasets.html", **_shell(
        "datasets", datasets=views, groups=catalog.GROUPS, group=group, q=q,
        how_steps=[{"n": n, "title": t, "desc": d, "color": c}
                   for n, t, d, c in catalog.HOW_STEPS],
    ))


@pe.post("/datasets/add")
@login_required
def add_dataset():
    name = request.form.get("dataset")
    sel = list(g.state.get("manual_selection") or [])
    if name and name not in sel:
        sel.append(name)
    g.state["manual_selection"] = sel
    _save_state()
    notify(f"{name} added to your extraction.")
    return redirect(url_for("pe.extract", tab="manual"))


# ══════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════
# cohort exposures
# ══════════════════════════════════════════════════════════════════════

def _cohort_collect_filters(cohort, form):
    return {col: form.getlist(f'filter__{col}') for col in cohort['filterable_columns']}


def _cohort_allowed_values(cohort, cohort_key):
    """Values of the cohort's access-scope column (e.g. Country) this
    account may query, for THIS specific cohort — an account can have a
    different country set per cohort (e.g. all 3 PRECISE countries but no
    DHS access at all). None = the cohort defines no such column (no
    restriction applies); otherwise a (possibly empty) list — empty means
    this account isn't scoped to anything yet for this cohort, even with
    cohort_access on."""
    scope_col = cohort.get("access_scope_column")
    if not scope_col:
        return None
    return auth.get_cohort_countries(g.username, cohort_key)


def _cohort_enforce_scope(cohort, cohort_key, filters):
    """Mutates filters in place so the access-scope column can never be
    widened beyond what this account is allowed for THIS cohort, no
    matter what the client actually submitted. Returns the enforced
    allowed-values list, or None if the cohort has no scope column."""
    scope_col = cohort.get("access_scope_column")
    allowed = _cohort_allowed_values(cohort, cohort_key)
    if allowed is None:
        return None
    submitted = [v for v in (filters.get(scope_col) or []) if v]
    filters[scope_col] = [v for v in submitted if v in allowed] or list(allowed)
    return allowed


def _cohort_collect_id_list(form):
    raw = (form.get('id_list') or '').strip()
    if not raw:
        return []
    return [tok.strip() for chunk in raw.splitlines() for tok in chunk.split(',') if tok.strip()]


def _cohort_collect_date_mode(form, prefix):
    date_mode = form.get(f'{prefix}_date_mode') or 'pregnancy_windows'
    ref_date_cols, custom_window, window_keys = {}, {}, []
    if date_mode == 'pregnancy_windows':
        window_keys = form.getlist(f'{prefix}_window_keys')
        ref_date_cols = {
            'conception': (form.get(f'{prefix}_conception_col') or '').strip(),
            'delivery': (form.get(f'{prefix}_delivery_col') or '').strip() or None,
        }
    else:
        ref_date_cols = {'index': (form.get(f'{prefix}_index_col') or '').strip()}
        custom_window = {
            'day_from': int(form.get(f'{prefix}_day_from') or 0),
            'day_to': int(form.get(f'{prefix}_day_to') or 0),
        }
    output_shape = 'long' if date_mode == 'case_crossover' else (form.get(f'{prefix}_output_shape') or 'wide')
    return date_mode, ref_date_cols, custom_window, window_keys, output_shape


@pe.route("/cohort/", methods=["GET"])
@login_required
@cohort_access_required
def cohort_assign():
    cohort_key = request.args.get("cohort", "precise")
    cohort = get_cohort(cohort_key)
    allowed_countries = _cohort_allowed_values(cohort, cohort_key)
    if allowed_countries is not None and not allowed_countries:
        notify("Your account isn't scoped to any country/site yet. Ask an admin on the Cohort admin page.", "error")
        return redirect(url_for("pe.home"))
    scope_col = cohort.get("access_scope_column")
    scope_filters = {scope_col: allowed_countries} if allowed_countries is not None else None
    scoped_cols = set(cohort.get("site_columns_scoped_by_country", []))
    filter_options = {
        col: cohort_query.distinct_values(cohort_key, col, filters=scope_filters)
        for col in cohort["filterable_columns"] if col not in scoped_cols
    }
    # Village/health_facility: tag each option with its own country so the
    # page can narrow these lists to whichever Country checkbox(es) the
    # user has selected, instead of mixing every country's sites together
    # regardless of what's picked.
    filter_options_scoped = {
        col: cohort_query.distinct_values_with_scope(cohort_key, col, filters=scope_filters)
        for col in scoped_cols
    }
    pending = g.state.get("cohort_pending_b")
    return render_template("cohort_assign.html", **_shell(
        "cohort",
        cohorts=list_cohorts(),
        cohort_key=cohort_key,
        cohort=cohort,
        filter_options=filter_options,
        filter_options_scoped=filter_options_scoped,
        allowed_countries=allowed_countries,
        pregnancy_windows=PREGNANCY_WINDOWS,
        datasets=catalog.all_views(),
        ghsl_years=DATASETS.get(GHSL_NAME, {}).get("years"),
        pregnancy_window_support=cohort.get("pregnancy_window_support", False),
        cohort_pending_b=pending,
    ))


@pe.route("/cohort/assign/existing", methods=["POST"])
@login_required
@cohort_access_required
def cohort_assign_existing():
    cohort_key = request.form.get("cohort", "precise")
    cohort = get_cohort(cohort_key)
    filters = _cohort_collect_filters(cohort, request.form)
    allowed = _cohort_enforce_scope(cohort, cohort_key, filters)
    if allowed is not None and not allowed:
        notify("Your account isn't scoped to any country/site yet. Ask an admin.", "error")
        return redirect(url_for("pe.cohort_assign", cohort=cohort_key))
    id_list = _cohort_collect_id_list(request.form)
    exposure_columns = request.form.getlist("exposure_columns")
    if not exposure_columns:
        notify("Pick at least one existing exposure variable.", "error")
        return redirect(url_for("pe.cohort_assign", cohort=cohort_key))

    date_mode, ref_date_cols, custom_window, window_keys, output_shape = \
        _cohort_collect_date_mode(request.form, "a")

    try:
        df = cohort_query.assign_existing_exposures(
            cohort_key, filters, id_list, exposure_columns,
            date_mode, ref_date_cols, window_keys, custom_window, output_shape,
        )
        out_dir = os.path.join(PALSEARTH_ROOT, "jobs")
        os.makedirs(out_dir, exist_ok=True)
        fname = f"{cohort_key}_existing_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.csv"
        out_path = os.path.join(out_dir, fname)
        df.to_csv(out_path, index=False)
        dest = cohort_delivery.deliver_file(g.username, out_path)
        cohort_delivery.record_delivery(f"sync-{fname}", g.username, dest, "delivered")
        notify(f"Done. {len(df)} rows written to ~/CohortExposures/{os.path.basename(dest)} "
               f"in your linked PALSlab Hub home directory.")
    except Exception as e:
        notify(f"Could not assign exposures: {e}", "error")
    return redirect(url_for("pe.cohort_assign", cohort=cohort_key))


@pe.route("/cohort/assign/new", methods=["POST"])
@login_required
@cohort_access_required
def cohort_assign_new():
    cohort_key = request.form.get("cohort", "precise")
    cohort = get_cohort(cohort_key)
    filters = _cohort_collect_filters(cohort, request.form)
    allowed = _cohort_enforce_scope(cohort, cohort_key, filters)
    if allowed is not None and not allowed:
        notify("Your account isn't scoped to any country/site yet. Ask an admin.", "error")
        return redirect(url_for("pe.cohort_assign", cohort=cohort_key))
    id_list = _cohort_collect_id_list(request.form)
    dataset_names = request.form.getlist("datasets")
    stats = request.form.getlist("stats") or ["mean"]
    if not dataset_names:
        notify("Pick at least one dataset to assign.", "error")
        return redirect(url_for("pe.cohort_assign", cohort=cohort_key))

    date_mode, ref_date_cols, custom_window, window_keys, output_shape = \
        _cohort_collect_date_mode(request.form, "b")

    # Same stat-feasibility check as the main Extract tool (see
    # core.ai_agent.check_stat_feasibility) — warns, never blocks, when the
    # requested stats can't come back as genuinely distinct numbers given
    # how these rows will actually be shaped, e.g. mean/min/max for
    # Precipitation - CHIRPS on single-day rows (CHIRPS only has one total
    # value per day). Deferred the same way submit_manual defers: stash the
    # full submitted form, show the warning, let them click "Run anyway".
    warnings = ai_agent.check_stat_feasibility(dataset_names, stats, date_mode, output_shape, DATASETS)
    if warnings and not request.form.get("confirm_stat_warning"):
        g.state["cohort_pending_b"] = {"form": request.form.to_dict(flat=False), "warnings": warnings}
        notify("Before this runs, see the note below, then click \"Run anyway\" if this is what you want.",
               "warn")
        return redirect(url_for("pe.cohort_assign", cohort=cohort_key))
    g.state.pop("cohort_pending_b", None)

    try:
        csv_path, n_participants = cohort_query.materialize_locations_csv(
            cohort_key, filters, id_list, ref_date_cols
        )
        job_id = jobs_db.create_job(
            username=g.username,
            datasets=dataset_names,
            start_date=None,
            end_date=None,
            geometry_type="point",
            input_filename=csv_path,
            ee_project="your-earth-engine-project",
            stats_requested=stats,
            output_format="csv",
            custom_datasets={},
            source="cohort_tool",
            date_mode=date_mode,
            window_keys=window_keys,
            ref_date_cols=ref_date_cols,
            custom_window=custom_window,
            output_shape=output_shape,
            temp_unit=request.form.get("b_temp_unit") or "kelvin",
        )
        run_job_in_background(job_id)
        cohort_delivery.queue_delivery(job_id, g.username, f"{cohort_key}: {', '.join(dataset_names)}")
        notify(f"Submitted. Assigning {len(dataset_names)} dataset(s) for {n_participants} participants. "
               f"It'll land in ~/CohortExposures/ once it's done.")
    except Exception as e:
        notify(f"Could not submit that job: {e}", "error")
    return redirect(url_for("pe.jobs"))


def _per_cohort_country_options(cohorts):
    """{cohort_key: [country, ...]} -- only computed for cohorts that
    actually define a scope column; a cohort with none needs no per-country
    grant at all. Shared by the admin grant page and the request-access
    form so both always offer the same real country list."""
    out = {}
    for c in cohorts:
        scope_col = get_cohort(c["key"]).get("access_scope_column")
        out[c["key"]] = cohort_query.distinct_values(c["key"], scope_col) if scope_col else []
    return out


@pe.route("/admin/cohort/", methods=["GET", "POST"])
@login_required
@cohort_admin_required
def admin_cohort():
    cohorts = list_cohorts()
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        if username:
            auth.set_cohort_access(username, bool(request.form.get("cohort_access")))
            for c in cohorts:
                auth.set_cohort_countries(
                    username, request.form.getlist(f"cohort_countries__{c['key']}"), cohort_key=c["key"])
            auth.set_hub_username(username, (request.form.get("hub_username") or "").strip())
            notify(f"Updated {username}.")
        return redirect(url_for("pe.admin_cohort"))
    users_raw = [dict(u) for u in auth.list_users()]
    cohort_countries_options = _per_cohort_country_options(cohorts)
    users = []
    for u in users_raw:
        u["cohort_country_access"] = auth.get_all_cohort_country_access(u["username"])
        users.append(u)
    return render_template("admin_cohort.html", **_shell(
        "cohort", users=users, cohorts=cohorts, cohort_countries_options=cohort_countries_options))


def create_app():
    app = Flask(__name__, static_url_path=f"{PREFIX}/static")
    app.secret_key = os.environ.get("PALSEARTH_SECRET_KEY") or os.urandom(32)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 1024 * 1024
    # Flash messages ride Flask's own signed session cookie.
    app.config.update(
        SESSION_COOKIE_NAME="palsearth_flash",
        SESSION_COOKIE_PATH=PREFIX or "/",
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=COOKIE_SECURE,
        SESSION_COOKIE_HTTPONLY=True,
    )
    app.register_blueprint(pe)
    cohort_delivery.start_watcher()

    @app.context_processor
    def _inject_notices():
        return {"notices": _take_notices}

    @app.url_defaults
    def _version_static(endpoint, values):
        if endpoint.endswith("static") and "filename" in values:
            try:
                path = os.path.join(app.static_folder, values["filename"])
                values.setdefault("v", int(os.path.getmtime(path)))
            except OSError:
                pass

    @app.after_request
    def cache_policy(resp):
        """
        Cloudflare sits in front of this host and has bitten this project
        before by caching pages it shouldn't. Every signed-in page is
        user-specific, so it must never be stored by a shared cache; only the
        fingerprint-free static assets are safe to cache, and only briefly.
        """
        if request.path.startswith(f"{PREFIX}/static/"):
            # URLs carry ?v=<mtime>, so a changed file is a new URL and these
            # can be cached hard — saves several round trips on every page.
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
            resp.headers["Pragma"] = "no-cache"
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp

    @app.get("/")
    def root():
        return redirect(url_for("pe.index"))

    @app.errorhandler(413)
    def too_big(_e):
        username, _ = _current_user()
        if username:
            st = state_store.load(username)
            st.setdefault("_flash", []).append(
                ["error", "That file is over the 200 MB limit."])
            state_store.save(username, st)
            return redirect(url_for("pe.extract"))
        return "That file is over the 200 MB limit.", 413

    return app


application = create_app()

if __name__ == "__main__":
    application.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8504)), debug=False)
