#!/usr/bin/env python3
"""
PRECISE DuckDB Access Request Service — port 35488
- /request            user submits an access request
- /status/<id>        user checks their request status
- /admin              admin dashboard (login required)
- /admin/login        admin login form
- /admin/approve      approve a request and issue API key
- /admin/reject       reject a request
"""
import sys, os, sqlite3, datetime, secrets
sys.path.insert(0, '/home/USER/PRECISE')

from flask import (Flask, request, jsonify, render_template_string,
                   redirect, url_for, session, flash)
import smtplib
from email.message import EmailMessage
from fpdf import FPDF
import bcrypt
import access_db

HUB_DB = '/var/lib/palslab-hub/jupyterhub.sqlite'

# ── JupyterHub account helpers ────────────────────────────────────────────────

def _hub_conn():
    con = sqlite3.connect(HUB_DB)
    con.row_factory = sqlite3.Row
    return con

def hub_get_users():
    """Return list of dicts for all hub users, newest first."""
    try:
        with _hub_conn() as con:
            rows = con.execute('''
                SELECT ui.username, ui.email, ui.is_authorized,
                       u.admin, u.created, u.last_activity
                FROM   users_info ui
                LEFT JOIN users u ON u.name = ui.username
                ORDER BY u.created DESC NULLS LAST
            ''').fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        return {'error': str(e)}

def hub_approve_user(username):
    try:
        with _hub_conn() as con:
            con.execute("UPDATE users_info SET is_authorized=1 WHERE username=?", (username,))
        return True, None
    except Exception as e:
        return False, str(e)

def hub_reject_user(username):
    try:
        with _hub_conn() as con:
            con.execute("DELETE FROM users_info WHERE username=?", (username,))
            con.execute("DELETE FROM users WHERE name=?", (username,))
        return True, None
    except Exception as e:
        return False, str(e)

def hub_create_user(username, email, password):
    """Create a pre-authorized hub account (NativeAuthenticator + JupyterHub users table)."""
    try:
        # NativeAuthenticator's users_info.password column is LargeBinary — it is
        # later passed straight back to bcrypt.hashpw(...) as the salt, which
        # requires raw bytes. Decoding to str here breaks that (TypeError on login).
        hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
        now = datetime.datetime.utcnow().isoformat()
        with _hub_conn() as con:
            # Insert into NativeAuthenticator table
            con.execute(
                "INSERT INTO users_info (username, password, is_authorized, email) VALUES (?,?,1,?)",
                (username, hashed, email)
            )
            # Insert into JupyterHub users table (required for the hub to recognise the user)
            con.execute(
                "INSERT OR IGNORE INTO users (name, admin, created) VALUES (?,0,?)",
                (username, now)
            )
        return True, None
    except sqlite3.IntegrityError:
        return False, 'Username already exists'
    except Exception as e:
        return False, str(e)

PALSEARTH_DB = '/home/USER/PRECISE/palsearth/palsearth_users.db'

# ── PALs Earth account helpers ────────────────────────────────────────────────
# Own Flask app (portal.placealert.org/palsearth/) with its own sqlite users
# table (core/auth.py there) — read/written directly here, same arm's-length
# pattern as the JupyterHub helpers above, so this dashboard needs nothing
# installed from that app's codebase.

def _palsearth_conn():
    con = sqlite3.connect(PALSEARTH_DB)
    con.row_factory = sqlite3.Row
    return con

def palsearth_get_pending_users():
    """Signups awaiting a decision — not yet approved, and not denied."""
    try:
        with _palsearth_conn() as con:
            rows = con.execute('''
                SELECT id, username, email, created_at
                FROM users
                WHERE is_approved = 0 AND is_denied = 0
                ORDER BY created_at ASC
            ''').fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        return {'error': str(e)}

def palsearth_get_user(username):
    try:
        with _palsearth_conn() as con:
            row = con.execute(
                "SELECT id, username, email FROM users WHERE username=?", (username,)
            ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None

def palsearth_approve_user(username):
    try:
        with _palsearth_conn() as con:
            cur = con.execute(
                "UPDATE users SET is_approved=1, is_denied=0 WHERE username=?", (username,)
            )
        return cur.rowcount > 0, None
    except Exception as e:
        return False, str(e)

def palsearth_deny_user(username):
    try:
        with _palsearth_conn() as con:
            cur = con.execute(
                "UPDATE users SET is_approved=0, is_denied=1 WHERE username=?", (username,)
            )
        return cur.rowcount > 0, None
    except Exception as e:
        return False, str(e)

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or os.urandom(32)

# All URLs behind /duckrequest/ nginx prefix
PREFIX = '/duckrequest'

SMTP_FROM   = 'admin@example.org'
SMTP_PASS   = os.environ.get('SMTP_APP_PASSWORD', '')
ADMIN_EMAILS = [
    'admin@example.org',
    'admin@example.org',           # bongani
    'admin@example.org',   # zororo
]

COUNTRIES = ['Kenya', 'Mozambique', 'Gambia']

# Requests submitted via geodata_api_request.py's /georequest/ form share this same
# SQLite table (tagged in the `purpose` column) so they show up here too — this API
# is pan-African / not country-restricted, so it needs its own approve path.
GEODATA_TAG = '[GEODATA-API] '
GEODATA_NO_COUNTRY_ACL = ['(none — pan-African geodata API, not country-restricted)']


def notify_geodata_user_approved(name, email, api_key):
    body = (
        f"Hi {name},\n\n"
        f"Your Roadnet/Euclidean API key has been approved:\n\n"
        f"  {api_key}\n\n"
        f"Send it as an X-API-Key header, e.g.:\n\n"
        f"  curl -X POST https://portal.placealert.org/precise-api/api/roadnet/query \\\n"
        f"    -H \"X-API-Key: {api_key}\" \\\n"
        f"    -F 'geojson={{\"type\":\"Polygon\",\"coordinates\":[[...]]}}'\n\n"
        f"Full docs (endpoints, shapefile upload, admin-unit lookups) are on the map "
        f"pages themselves \u2014 click the \"API\" button:\n"
        f"  https://portal.placealert.org/roadnet/\n"
        f"  https://portal.placealert.org/euclidean/\n\n"
        f"\u2014 PALs"
    )
    _send(email, 'Your Roadnet/Euclidean API key', body)

CATALOGUE_CODE = os.environ.get('CATALOGUE_ACCESS_CODE', 'PRECISE2024')
HE2AT_CODE     = os.environ.get('HE2AT_ACCESS_CODE', 'HE2AT2022')


# ── Email helpers ─────────────────────────────────────────────────────────────

def _send(to, subject, body, attachments=None):
    """Send a plain-text email with optional (filename, bytes, subtype) attachments."""
    try:
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From']    = SMTP_FROM
        msg['To']      = to
        msg.set_content(body)
        if attachments:
            for fname, data, subtype in attachments:
                msg.add_attachment(data, maintype='application',
                                   subtype=subtype, filename=fname)
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(SMTP_FROM, SMTP_PASS)
            s.send_message(msg)
    except Exception:
        pass


def _build_instructions_pdf(name, api_key, countries):
    """Return PDF bytes containing the access instructions for this user."""
    countries_str = ', '.join(countries)

    r_snippet = (
        f'library(httr)\n\n'
        f'resp <- POST(\n'
        f'  "http://localhost:5000/api/query/csv",\n'
        f'  add_headers("X-API-Key" = "{api_key}"),\n'
        f'  body = list(sql = "SELECT * FROM daily_data"),\n'
        f'  encode = "json"\n'
        f')\n\n'
        f'tmp <- tempfile(fileext = ".csv")\n'
        f'writeBin(content(resp, "raw"), tmp)\n'
        f'df <- read.csv(tmp)\n\n'
        f'dim(df)   # rows x columns\n'
        f'head(df)  # first 6 rows'
    )

    py_snippet = (
        f'import sys\n'
        f'sys.path.insert(0, "/home/USER/PRECISE")\n'
        f'from precise_db import PreciseDB\n\n'
        f'db = PreciseDB(api_key="{api_key}")\n'
        f'df = db.query("SELECT * FROM daily_data")\n'
        f'db.close()\n\n'
        f'# df is a pandas DataFrame ready for analysis'
    )

    pdf = FPDF()
    pdf.set_margins(20, 20, 20)
    pdf.add_page()

    # Title
    pdf.set_font('Helvetica', 'B', 18)
    pdf.set_text_color(26, 83, 92)   # #1a535c
    pdf.cell(0, 10, 'PRECISE DuckDB — Access Instructions', ln=True)
    pdf.ln(2)

    # Subtitle
    pdf.set_font('Helvetica', '', 11)
    pdf.set_text_color(107, 114, 128)
    pdf.cell(0, 7, 'PALS Lab Team  |  placealert.org', ln=True)
    pdf.ln(6)

    # User details box
    pdf.set_fill_color(240, 253, 244)
    pdf.set_draw_color(16, 185, 129)
    pdf.set_font('Helvetica', 'B', 10)
    pdf.set_text_color(55, 65, 81)
    pdf.cell(0, 8, f'Name: {name}', ln=True, fill=True)
    pdf.cell(0, 8, f'Approved countries: {countries_str}', ln=True, fill=True)
    pdf.ln(2)

    # API Key
    pdf.set_font('Helvetica', 'B', 10)
    pdf.set_text_color(55, 65, 81)
    pdf.cell(0, 7, 'Your API Key:', ln=True)
    pdf.set_font('Courier', '', 9)
    pdf.set_fill_color(243, 244, 246)
    pdf.set_text_color(17, 24, 39)
    pdf.cell(0, 8, api_key, ln=True, fill=True)
    pdf.set_font('Helvetica', 'I', 9)
    pdf.set_text_color(156, 163, 175)
    pdf.cell(0, 6, 'Keep this key private. Do not share it.', ln=True)
    pdf.ln(6)

    # Divider
    pdf.set_draw_color(226, 232, 240)
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(5)

    def section(title, code):
        pdf.set_font('Helvetica', 'B', 12)
        pdf.set_text_color(26, 83, 92)
        pdf.cell(0, 8, title, ln=True)
        pdf.ln(2)
        pdf.set_fill_color(15, 23, 42)
        pdf.set_text_color(205, 214, 244)
        pdf.set_font('Courier', '', 8.5)
        # multi-line code block
        pdf.multi_cell(0, 5.5, code, fill=True)
        pdf.ln(6)

    section('PYTHON  (PALSlab Hub notebook)', py_snippet)

    pdf.set_draw_color(226, 232, 240)
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(5)

    section('R  (PALSlab Hub notebook)', r_snippet)

    # Footer note
    pdf.set_font('Helvetica', 'I', 9)
    pdf.set_text_color(107, 114, 128)
    pdf.multi_cell(0, 5,
        'R note: Uses the CSV endpoint to avoid R memory limits on large tables. '
        'httr is a standard R package. Remove LIMIT or adjust the query as needed.'
    )

    return bytes(pdf.output())


def notify_admin(req_id, name, email, institution, hub_user, countries, purpose):
    countries_str = ', '.join(countries)
    body = (
        f'A new DuckDB access request requires your approval.\n\n'
        f'Name:         {name}\n'
        f'Email:        {email}\n'
        f'Institution:  {institution}\n'
        f'Hub Username: {hub_user}\n'
        f'Countries:    {countries_str}\n\n'
        f'Purpose:\n{purpose}\n\n'
        f'Review at: https://placealert.org/duckrequest/admin\n'
        f'Request ID: {req_id}'
    )
    for addr in ADMIN_EMAILS:
        _send(addr, f'[PRECISE DuckDB] New access request from {name}', body)


def notify_user_approved(name, email, api_key, countries):
    countries_str = ', '.join(countries)
    body = (
        f'Hi {name},\n\n'
        f'Your request for access to the PRECISE DuckDB has been approved.\n\n'
        f'Approved countries: {countries_str}\n\n'
        f'Your API key:\n'
        f'  {api_key}\n\n'
        f'Keep this key private.\n\n'
        f'━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n'
        f'PYTHON (PALSlab Hub notebook)\n\n'
        f'  import sys\n'
        f'  sys.path.insert(0, "/home/USER/PRECISE")\n'
        f'  from precise_db import PreciseDB\n\n'
        f'  db = PreciseDB(api_key="{api_key}")\n'
        f'  df = db.query("SELECT * FROM daily_data")\n'
        f'  db.close()\n\n'
        f'  # df is a pandas DataFrame ready for analysis\n\n'
        f'━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n'
        f'R (PALSlab Hub notebook)\n\n'
        f'  library(httr)\n'
        f'  library(jsonlite)\n\n'
        f'  resp <- POST(\n'
        f'    "http://localhost:5000/api/query/csv",\n'
        f'    add_headers("X-API-Key" = "{api_key}"),\n'
        f'    body = list(sql = "SELECT * FROM daily_data"),\n'
        f'    encode = "json"\n'
        f'  )\n\n'
        f'  tmp <- tempfile(fileext = ".csv")\n'
        f'  writeBin(content(resp, "raw"), tmp)\n'
        f'  df <- read.csv(tmp)\n\n'
        f'  dim(df)   # rows x columns\n'
        f'  head(df)  # first 6 rows\n\n'
        f'━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n'
        f'PALS Lab Team\n\n'
        f'A PDF copy of these instructions is attached.'
    )
    try:
        pdf_bytes = _build_instructions_pdf(name, api_key, countries)
        attachments = [('PRECISE_Access_Instructions.pdf', pdf_bytes, 'pdf')]
    except Exception as e:
        app.logger.error(f'PDF generation failed: {e}')
        attachments = None
    _send(email, '[PRECISE DuckDB] Your access request has been approved',
          body, attachments=attachments)


def notify_user_rejected(name, email, notes):
    _send(
        email,
        '[PRECISE DuckDB] Your access request was not approved',
        f'Hi {name},\n\n'
        f'Unfortunately your request for access to the PRECISE DuckDB '
        f'was not approved at this time.\n\n'
        + (f'Reason: {notes}\n\n' if notes else '')
        + f'You may submit a new request if your circumstances change.\n\n'
          f'PALS Lab Team'
    )


# ── Shared CSS ────────────────────────────────────────────────────────────────

BASE_STYLE = '''
<style>
* { box-sizing:border-box; margin:0; padding:0; }
body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       background:#f0f4f8; min-height:100vh; padding:32px 16px; }
.page { max-width:580px; margin:0 auto; display:flex; flex-direction:column; gap:20px; }
.page.wide { max-width:900px; }
.card { background:white; border-radius:12px;
        box-shadow:0 4px 24px rgba(0,0,0,.08); padding:36px 40px; }
.logo  { font-size:22px; font-weight:700; color:#1a535c; margin-bottom:4px; }
.sub   { color:#6b7280; font-size:13px; margin-bottom:22px; }
.intro { background:#f0fdf4; border-left:3px solid #10b981;
         padding:12px 14px; border-radius:0 8px 8px 0;
         font-size:13px; color:#374151; margin-bottom:20px; line-height:1.6; }
.warn  { background:#fffbeb; border-left:3px solid #f59e0b;
         padding:12px 14px; border-radius:0 8px 8px 0;
         font-size:13px; color:#92400e; margin-bottom:20px; line-height:1.6; }
label  { display:block; font-size:13px; font-weight:600; color:#374151;
         margin:14px 0 5px; }
input,select,textarea {
  width:100%; padding:10px 12px; border:1px solid #d1d5db;
  border-radius:8px; font-size:14px; color:#111827; outline:none; }
input:focus,select:focus,textarea:focus {
  border-color:#10b981; box-shadow:0 0 0 3px rgba(16,185,129,.1); }
textarea { resize:vertical; min-height:70px; }
.check-group { display:flex; flex-wrap:wrap; gap:10px; margin-top:6px; }
.check-group label { margin:0; font-weight:400; display:flex; align-items:center;
                     gap:6px; cursor:pointer; }
.check-group input[type=checkbox] { width:auto; }
.btn { padding:11px 20px; border:none; border-radius:8px;
       font-size:14px; font-weight:600; cursor:pointer; transition:background .2s; }
.btn-primary   { background:#1a535c; color:white; width:100%; margin-top:20px; }
.btn-primary:hover   { background:#10b981; }
.btn-approve   { background:#10b981; color:white; }
.btn-approve:hover   { background:#059669; }
.btn-reject    { background:#ef4444; color:white; }
.btn-reject:hover    { background:#dc2626; }
.btn-sm { padding:6px 14px; font-size:13px; }
.btn:disabled { background:#9ca3af; cursor:not-allowed; }
.alert { padding:14px 16px; border-radius:8px; font-size:14px; margin-top:16px; }
.alert-ok   { background:#ecfdf5; border:1px solid #6ee7b7; color:#065f46; }
.alert-err  { background:#fef2f2; border:1px solid #fca5a5; color:#991b1b; }
.alert-info { background:#eff6ff; border:1px solid #93c5fd; color:#1e40af; }
.badge { display:inline-block; padding:2px 10px; border-radius:20px;
         font-size:12px; font-weight:600; }
.badge-pending  { background:#fef3c7; color:#92400e; }
.badge-approved { background:#d1fae5; color:#065f46; }
.badge-rejected { background:#fee2e2; color:#991b1b; }
.snippet { background:#0f172a; border-radius:10px; padding:16px;
           font-family:monospace; font-size:12.5px; color:#cdd6f4;
           line-height:1.7; white-space:pre-wrap; overflow-x:auto;
           margin-top:16px; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th { text-align:left; padding:10px 12px; background:#f8fafc;
     border-bottom:2px solid #e2e8f0; color:#374151; font-weight:600; }
td { padding:10px 12px; border-bottom:1px solid #f1f5f9; color:#374151; vertical-align:top; }
tr:hover td { background:#f8fafc; }
.back { text-align:center; font-size:13px; color:#6b7280; }
.back a { color:#1a535c; text-decoration:none; font-weight:600; }
.flash { padding:12px 16px; border-radius:8px; font-size:14px;
         margin-bottom:16px; }
.flash-ok  { background:#ecfdf5; color:#065f46; border:1px solid #6ee7b7; }
.flash-err { background:#fef2f2; color:#991b1b; border:1px solid #fca5a5; }
.modal-backdrop {
  display:none; position:fixed; inset:0; background:rgba(0,0,0,.5);
  z-index:1000; align-items:center; justify-content:center; }
.modal-backdrop.open { display:flex; }
.modal { background:white; border-radius:12px; padding:32px;
         max-width:440px; width:90%; box-shadow:0 25px 60px rgba(0,0,0,.3); }
.modal h3 { margin-bottom:14px; color:#1a535c; }
.modal-footer { display:flex; gap:10px; margin-top:20px; justify-content:flex-end; }
</style>
'''


# ══════════════════════════════════════════════════════════════════════════════
# USER ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/')
@app.route('/request')
def request_form():
    return render_template_string(BASE_STYLE + '''
<div class="page">
 <div class="card">
  <div class="logo">PRECISE DuckDB</div>
  <div class="sub">PRECISE Network — Access Request</div>
  <div class="intro">
    Fill in the form below to request access to the PRECISE Big Table dataset via
    <strong>DuckDB</strong>. Your request will be reviewed by the admin and you will
    receive an email once a decision has been made — the same process as for PALSlab Hub.
  </div>
  <div class="warn">
    Access is granted per country. You will only be able to query data for the
    countries approved in your request.
  </div>
  <form id="f">
    <label>Full Name</label>
    <input id="name" type="text" required placeholder="e.g. Jane Researcher"/>
    <label>Email Address</label>
    <input id="email" type="email" required placeholder="you@institution.org"/>
    <label>Institution</label>
    <input id="inst" type="text" required placeholder="e.g. CESHHAR / University of..."/>
    <label>PALSlab Hub Username</label>
    <input id="hub" type="text" required placeholder="Your JupyterHub login username"/>
    <label>Countries Needed</label>
    <div class="check-group">
      <label><input type="checkbox" name="country" value="Kenya"/> Kenya</label>
      <label><input type="checkbox" name="country" value="Mozambique"/> Mozambique</label>
      <label><input type="checkbox" name="country" value="Gambia"/> The Gambia</label>
    </div>
    <label>Purpose / Research Question</label>
    <textarea id="purpose" placeholder="Describe what you plan to analyse..."></textarea>
    <button class="btn btn-primary" type="submit" id="sub">Submit Request</button>
  </form>
  <div id="msg"></div>
 </div>
 <div class="back"><a href="/">← Back to PALS Lab Portal</a></div>
</div>
<script>
document.getElementById('f').addEventListener('submit', async e => {
  e.preventDefault();
  const btn = document.getElementById('sub');
  btn.disabled = true; btn.textContent = 'Submitting…';
  const countries = [...document.querySelectorAll('input[name=country]:checked')].map(x=>x.value);
  if (!countries.length) {
    document.getElementById('msg').innerHTML =
      '<div class="alert alert-err">Please select at least one country.</div>';
    btn.disabled=false; btn.textContent='Submit Request'; return;
  }
  const r = await fetch('submit', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      name: document.getElementById('name').value.trim(),
      email: document.getElementById('email').value.trim(),
      institution: document.getElementById('inst').value.trim(),
      hub_user: document.getElementById('hub').value.trim(),
      purpose: document.getElementById('purpose').value.trim(),
      countries
    })
  });
  const d = await r.json();
  if (d.ok) {
    document.getElementById('f').style.display='none';
    document.getElementById('msg').innerHTML =
      '<div class="alert alert-ok"><strong>Request submitted!</strong><br>' +
      'You will receive an email when your request has been reviewed.<br><br>' +
      'Your request ID: <code style="background:#f3f4f6;padding:2px 6px;border-radius:4px">'
      + d.request_id + '</code><br>' +
      '<small>Save this to check your status at <a href="status/'
      + d.request_id + '">/duckrequest/status/' + d.request_id + '</a></small></div>';
  } else {
    document.getElementById('msg').innerHTML =
      '<div class="alert alert-err">Error: ' + (d.error||'unknown') + '</div>';
    btn.disabled=false; btn.textContent='Submit Request';
  }
});
</script>
''')


@app.route('/submit', methods=['POST'])
def submit():
    d = request.get_json(force=True)
    name        = d.get('name','').strip()
    email       = d.get('email','').strip()
    institution = d.get('institution','').strip()
    hub_user    = d.get('hub_user','').strip()
    purpose     = d.get('purpose','').strip()
    countries   = [c for c in d.get('countries',[]) if c in COUNTRIES]

    if not all([name, email, institution, hub_user, countries]):
        return jsonify({'ok': False, 'error': 'Missing required fields or no valid countries'})

    req_id = access_db.create_request(name, email, institution, hub_user, purpose, countries)
    notify_admin(req_id, name, email, institution, hub_user, countries, purpose)

    return jsonify({'ok': True, 'request_id': req_id})


@app.route('/status/<req_id>')
def status(req_id):
    req = access_db.get_request_with_key(req_id)
    if not req:
        return render_template_string(BASE_STYLE + '''
<div class="page"><div class="card">
  <div class="logo">PRECISE DuckDB</div>
  <div class="alert alert-err">Request ID not found.</div>
  <div class="back" style="margin-top:16px"><a href="/duckrequest/request">← Make a request</a></div>
</div></div>'''), 404

    status_label = {
        'pending':  '<span class="badge badge-pending">Pending review</span>',
        'approved': '<span class="badge badge-approved">Approved</span>',
        'rejected': '<span class="badge badge-rejected">Rejected</span>',
    }.get(req['status'], req['status'])

    snippet = ''
    if req['status'] == 'approved':
        key = req.get('api_key','')
        countries = req.get('countries', [])
        c_str = ', '.join(countries)
        snippet = f'''
<div style="display:flex;gap:8px;margin-bottom:0">
  <button onclick="showTab('py')" id="tab-py"
    style="padding:6px 16px;border:none;border-radius:6px 6px 0 0;cursor:pointer;background:#1e293b;color:#f8fafc;font-weight:600">Python</button>
  <button onclick="showTab('r')" id="tab-r"
    style="padding:6px 16px;border:none;border-radius:6px 6px 0 0;cursor:pointer;background:#334155;color:#94a3b8">R</button>
</div>
<div id="snippet-py" class="snippet" style="margin-top:0;border-radius:0 6px 6px 6px">import sys
sys.path.insert(0, "/home/USER/PRECISE")
from precise_db import PreciseDB

db = PreciseDB(api_key="{key}")
df = db.query("SELECT * FROM daily_data")
db.close()

# df is a pandas DataFrame — use it for your analysis</div>
<div id="snippet-r" class="snippet" style="display:none;margin-top:0;border-radius:0 6px 6px 6px">library(httr)
library(jsonlite)

resp &lt;- POST(
  "http://localhost:5000/api/query",
  add_headers("X-API-Key" = "{key}"),
  content_type_json(),
  body = '{{"sql": "SELECT * FROM daily_data"}}'
)

result &lt;- fromJSON(content(resp, as = "text"))
df     &lt;- as.data.frame(result$rows)
colnames(df) &lt;- result$columns

# df is a data.frame — use it for your analysis</div>
<script>
function showTab(lang) {{
  document.getElementById('snippet-py').style.display = lang==='py' ? 'block' : 'none';
  document.getElementById('snippet-r').style.display  = lang==='r'  ? 'block' : 'none';
  document.getElementById('tab-py').style.background  = lang==='py' ? '#1e293b' : '#334155';
  document.getElementById('tab-py').style.color       = lang==='py' ? '#f8fafc' : '#94a3b8';
  document.getElementById('tab-r').style.background   = lang==='r'  ? '#1e293b' : '#334155';
  document.getElementById('tab-r').style.color        = lang==='r'  ? '#f8fafc' : '#94a3b8';
}}
</script>'''

    return render_template_string(BASE_STYLE + '''
<div class="page">
 <div class="card">
  <div class="logo">PRECISE DuckDB — Request Status</div>
  <div class="sub">Request ID: <code>{{ req_id }}</code></div>
  <table style="margin-bottom:16px">
    <tr><th>Name</th><td>{{ req.name }}</td></tr>
    <tr><th>Email</th><td>{{ req.email }}</td></tr>
    <tr><th>Countries requested</th><td>{{ req.countries_req | join(', ') }}</td></tr>
    <tr><th>Submitted</th><td>{{ req.created_at[:19] | replace("T"," ") }} UTC</td></tr>
    <tr><th>Status</th><td>{{ status_label | safe }}</td></tr>
    {% if req.reviewed_at %}
    <tr><th>Reviewed</th><td>{{ req.reviewed_at[:19] | replace("T"," ") }} UTC</td></tr>
    {% endif %}
    {% if req.notes %}
    <tr><th>Notes</th><td>{{ req.notes }}</td></tr>
    {% endif %}
  </table>
  {% if req.status == 'approved' %}
  <div class="intro">Your API key has been issued. Keep it private.
  {% if req.api_key %}
  <br><br><strong>API Key:</strong>
  <code style="display:block;margin-top:6px;background:#f3f4f6;padding:8px 10px;
               border-radius:6px;word-break:break-all;font-size:12px">{{ req.api_key }}</code>
  {% endif %}
  </div>
  {{ snippet | safe }}
  {% elif req.status == 'pending' %}
  <div class="warn">Your request is awaiting review. You will receive an email once a decision has been made.</div>
  {% elif req.status == 'rejected' %}
  <div class="alert alert-err">Your request was not approved. {{ req.notes or '' }}</div>
  {% endif %}
 </div>
 <div class="back"><a href="/duckrequest/request">← Submit a new request</a></div>
</div>
''', req=req, req_id=req_id, status_label=status_label, snippet=snippet)


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN ROUTES
# ══════════════════════════════════════════════════════════════════════════════

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin'):
            return redirect(PREFIX + '/admin/login')
        return f(*args, **kwargs)
    return decorated


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    error = ''
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        ok, must_change = access_db.check_admin(username, password)
        if ok:
            if must_change:
                session['pending_admin'] = username
                return redirect(PREFIX + '/admin/change-password?first=1')
            session['admin'] = True
            session['admin_user'] = username
            return redirect(PREFIX + '/admin')
        error = 'Incorrect username or password.'
    return render_template_string(BASE_STYLE + '''
<div class="page" style="max-width:380px;margin:80px auto">
 <div class="card">
  <div class="logo">PRECISE DuckDB</div>
  <div class="sub">Admin — Login</div>
  {% if error %}<div class="alert alert-err">{{ error }}</div>{% endif %}
  <form method="post">
    <label>Username</label>
    <input type="text" name="username" autofocus autocomplete="username" placeholder="username"/>
    <label>Password</label>
    <input type="password" name="password" autocomplete="current-password"/>
    <button class="btn btn-primary" type="submit">Login</button>
  </form>
  <p style="margin-top:1rem;font-size:13px;text-align:center">
    <a href="{{ prefix }}/admin/forgot-password" style="color:var(--teal)">Forgot password?</a>
  </p>
 </div>
</div>
''', error=error, prefix=PREFIX)


@app.route('/admin/forgot-password', methods=['GET', 'POST'])
def admin_forgot_password():
    message = ''
    alert   = ''
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email    = access_db.get_admin_email(username)
        message  = 'If that username is registered, a reset link has been sent to the associated email address.'
        alert    = 'alert-info'
        if email:
            token     = access_db.create_admin_reset_token(username)
            reset_url = f'https://placealert.org/duckrequest/admin/reset-password?token={token}'
            _send(
                email,
                '[PRECISE DuckDB] Password reset request',
                f'Hi {username},\n\n'
                f'A password reset was requested for your PRECISE DuckDB admin account.\n'
                f'Click the link below within 1 hour to set a new password:\n\n'
                f'    {reset_url}\n\n'
                f'If you did not request this, ignore this email.\n\n'
                f'— Place Alert Labs'
            )
    return render_template_string(BASE_STYLE + '''
<div class="page" style="max-width:400px;margin:80px auto">
 <div class="card">
  <div class="logo">PRECISE DuckDB</div>
  <div class="sub">Reset Password</div>
  {% if message %}<div class="alert {{ alert }}">{{ message }}</div>{% endif %}
  {% if not message %}
  <p style="font-size:14px;color:#666;margin-bottom:1.25rem">
    Enter your admin username and we will email you a reset link.
  </p>
  <form method="post">
    <label>Username</label>
    <input type="text" name="username" autofocus placeholder="username"/>
    <button class="btn btn-primary" type="submit">Send reset link</button>
  </form>
  {% endif %}
  <p style="margin-top:1rem;font-size:13px;text-align:center">
    <a href="{{ prefix }}/admin/login" style="color:var(--teal)">Back to login</a>
  </p>
 </div>
</div>
''', message=message, alert=alert, prefix=PREFIX)


@app.route('/admin/reset-password', methods=['GET', 'POST'])
def admin_reset_password():
    token   = request.args.get('token', '') or request.form.get('token', '')
    message = ''
    alert   = ''
    done    = False
    username = access_db.verify_admin_reset_token(token)
    valid   = username is not None

    if request.method == 'POST' and valid:
        new_pw  = request.form.get('new_password', '')
        confirm = request.form.get('confirm_password', '')
        if not new_pw or len(new_pw) < 8:
            message, alert = 'Password must be at least 8 characters.', 'alert-err'
        elif new_pw != confirm:
            message, alert = 'Passwords do not match.', 'alert-err'
        else:
            access_db.consume_admin_reset_token(token, new_pw)
            message, alert, done = 'Password updated. You can now log in.', 'alert-ok', True
            valid = False

    if not valid and not done:
        message = message or 'This reset link is invalid or has expired.'
        alert   = alert   or 'alert-err'

    return render_template_string(BASE_STYLE + '''
<div class="page" style="max-width:400px;margin:80px auto">
 <div class="card">
  <div class="logo">PRECISE DuckDB</div>
  <div class="sub">Set New Password</div>
  {% if message %}<div class="alert {{ alert }}">{{ message }}</div>{% endif %}
  {% if valid %}
  <form method="post">
    <input type="hidden" name="token" value="{{ token }}"/>
    <label>New password</label>
    <input type="password" name="new_password" autofocus autocomplete="new-password"
           placeholder="At least 8 characters"/>
    <label>Confirm password</label>
    <input type="password" name="confirm_password" autocomplete="new-password"/>
    <button class="btn btn-primary" type="submit">Set password</button>
  </form>
  {% endif %}
  <p style="margin-top:1rem;font-size:13px;text-align:center">
    <a href="{{ prefix }}/admin/login" style="color:var(--teal)">Back to login</a>
  </p>
 </div>
</div>
''', token=token, valid=valid, message=message, alert=alert, prefix=PREFIX)


@app.route('/admin/change-password', methods=['GET', 'POST'])
def admin_change_password():
    """For first-time setup (pending_admin) or logged-in admins wanting to change password."""
    first      = request.args.get('first') == '1'
    pending    = session.get('pending_admin')
    logged_in  = session.get('admin')

    if not pending and not logged_in:
        return redirect(PREFIX + '/admin/login')

    username = pending or session.get('admin_user', '')
    message  = ''
    alert    = ''

    if request.method == 'POST':
        new_pw  = request.form.get('new_password', '')
        confirm = request.form.get('confirm_password', '')
        # For logged-in (non-first-time) require current password
        if logged_in and not pending:
            current = request.form.get('current_password', '')
            ok, _   = access_db.check_admin(username, current)
            if not ok:
                message, alert = 'Current password is incorrect.', 'alert-err'
        if not message:
            if not new_pw or len(new_pw) < 8:
                message, alert = 'New password must be at least 8 characters.', 'alert-err'
            elif new_pw != confirm:
                message, alert = 'Passwords do not match.', 'alert-err'
            else:
                access_db.set_admin_password(username, new_pw)
                session.pop('pending_admin', None)
                session['admin']      = True
                session['admin_user'] = username
                return redirect(PREFIX + '/admin')

    return render_template_string(BASE_STYLE + '''
<div class="page" style="max-width:400px;margin:80px auto">
 <div class="card">
  <div class="logo">PRECISE DuckDB</div>
  <div class="sub">{% if first %}Set Your Password{% else %}Change Password{% endif %}</div>
  {% if first %}
  <p style="font-size:14px;color:#666;margin-bottom:1.25rem">
    Welcome, <strong>{{ username }}</strong>. Please set a personal password before continuing.
  </p>
  {% endif %}
  {% if message %}<div class="alert {{ alert }}">{{ message }}</div>{% endif %}
  <form method="post">
    {% if not first %}
    <label>Current password</label>
    <input type="password" name="current_password" autocomplete="current-password"/>
    {% endif %}
    <label>New password</label>
    <input type="password" name="new_password" autofocus autocomplete="new-password"
           placeholder="At least 8 characters"/>
    <label>Confirm new password</label>
    <input type="password" name="confirm_password" autocomplete="new-password"/>
    <button class="btn btn-primary" type="submit">
      {% if first %}Set password &amp; continue{% else %}Update password{% endif %}
    </button>
  </form>
  {% if not first %}
  <p style="margin-top:1rem;font-size:13px;text-align:center">
    <a href="{{ prefix }}/admin" style="color:var(--teal)">Cancel</a>
  </p>
  {% endif %}
 </div>
</div>
''', first=first, username=username, message=message, alert=alert, prefix=PREFIX)


@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(PREFIX + '/admin/login')


@app.route('/admin')
@admin_required
def admin_dashboard():
    pending  = access_db.get_requests('pending')
    approved = access_db.get_requests('approved')
    rejected = access_db.get_requests('rejected')
    revoked  = access_db.get_requests('revoked')

    prec_pending  = access_db.get_catalogue_requests(catalogue='precise', status='pending')
    prec_approved = access_db.get_catalogue_requests(catalogue='precise', status='approved')
    prec_rejected = access_db.get_catalogue_requests(catalogue='precise', status='rejected')
    he2at_pending  = access_db.get_catalogue_requests(catalogue='he2at', status='pending')
    he2at_approved = access_db.get_catalogue_requests(catalogue='he2at', status='approved')
    he2at_rejected = access_db.get_catalogue_requests(catalogue='he2at', status='rejected')

    def rows(reqs, mode):
        # mode: 'pending' | 'approved' | 'other'
        out = ''
        for r in reqs:
            badge = {
                'pending':  '<span class="badge badge-pending">Pending</span>',
                'approved': '<span class="badge badge-approved">Approved</span>',
                'rejected': '<span class="badge badge-rejected">Rejected</span>',
                'revoked':  '<span class="badge" style="background:#f3e8ff;color:#6b21a8">Revoked</span>',
            }.get(r['status'], r['status'])
            is_geo  = (r.get('purpose') or '').startswith(GEODATA_TAG)
            cntrs   = 'Pan-African API \u2014 no country restriction' if is_geo else ', '.join(r['countries_req'])
            date    = r['created_at'][:10]
            actions = ''
            if mode == 'pending':
                actions = (
                    f'<button class="btn btn-approve btn-sm" '
                    f'onclick="openApprove(\'{r["id"]}\',\'{r["name"]}\','
                    f'{r["countries_req"]},{str(is_geo).lower()})">Approve</button>'
                    f'<button class="btn btn-reject btn-sm" '
                    f'onclick="openReject(\'{r["id"]}\',\'{r["name"]}\')" '
                    f'style="margin-left:6px">Reject</button>'
                )
            elif mode == 'approved':
                actions = (
                    f'<button class="btn btn-sm" '
                    f'style="background:#7c3aed;color:white" '
                    f'onclick="openRevoke(\'{r["id"]}\',\'{r["name"]}\')">Revoke</button>'
                )
            out += f'''<tr>
              <td>{date}</td>
              <td>{r["name"]}<br><small style="color:#6b7280">{r["email"]}</small></td>
              <td><small>{r["institution"]}</small></td>
              <td>{cntrs}</td>
              <td>{badge}</td>
              <td>{actions}</td>
            </tr>'''
        return out or '<tr><td colspan="6" style="color:#9ca3af;text-align:center">None</td></tr>'

    def cat_rows(reqs, mode):
        out = ''
        for r in reqs:
            badge = {
                'pending':  '<span class="badge badge-pending">Pending</span>',
                'approved': '<span class="badge badge-approved">Approved</span>',
                'rejected': '<span class="badge badge-rejected">Rejected</span>',
            }.get(r['status'], r['status'])
            date    = r['created_at'][:10]
            actions = ''
            if mode == 'pending':
                actions = (
                    f'<button class="btn btn-approve btn-sm" '
                    f'onclick="openCatApprove(\'{r["id"]}\',\'{r["name"]}\',\'{r["catalogue"]}\')">Approve</button>'
                    f'<button class="btn btn-reject btn-sm" style="margin-left:6px" '
                    f'onclick="openCatReject(\'{r["id"]}\',\'{r["name"]}\')">Reject</button>'
                )
            out += f'''<tr>
              <td>{date}</td>
              <td>{r["name"]}<br><small style="color:#6b7280">{r["email"]}</small></td>
              <td><small>{r.get("institution","")}</small></td>
              <td>{badge}</td>
              <td>{actions}</td>
            </tr>'''
        return out or '<tr><td colspan="5" style="color:#9ca3af;text-align:center">None</td></tr>'

    return render_template_string(BASE_STYLE + '''
<div class="page wide" style="margin:0 auto;padding:32px 24px">
 <div class="card">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
    <div>
      <div class="logo">PRECISE DuckDB — Admin</div>
      <div class="sub" style="margin-bottom:0">Access request management</div>
    </div>
    <a href="{{ prefix }}/admin/change-password" style="color:var(--teal);font-size:13px;text-decoration:none;margin-right:1rem">Change password</a>
    <a href="{{ prefix }}/admin/logout" style="color:#ef4444;font-size:13px;text-decoration:none">Logout</a>
  </div>

  <h3 style="margin-bottom:12px;color:#1a535c">
    Pending <span class="badge badge-pending">{{ pending|length }}</span>
  </h3>
  <table><tr>
    <th>Date</th><th>Requester</th><th>Institution</th>
    <th>Countries</th><th>Status</th><th>Actions</th>
  </tr>{{ pending_rows | safe }}</table>

  <h3 style="margin:28px 0 12px;color:#1a535c">
    Approved <span class="badge badge-approved">{{ approved|length }}</span>
  </h3>
  <table><tr>
    <th>Date</th><th>Requester</th><th>Institution</th>
    <th>Countries</th><th>Status</th><th>Actions</th>
  </tr>{{ approved_rows | safe }}</table>

  <h3 style="margin:28px 0 12px;color:#1a535c">
    Revoked <span class="badge" style="background:#f3e8ff;color:#6b21a8">{{ revoked|length }}</span>
  </h3>
  <table><tr>
    <th>Date</th><th>Requester</th><th>Institution</th>
    <th>Countries</th><th>Status</th><th></th>
  </tr>{{ revoked_rows | safe }}</table>

  <h3 style="margin:28px 0 12px;color:#1a535c">
    Rejected <span class="badge badge-rejected">{{ rejected|length }}</span>
  </h3>
  <table><tr>
    <th>Date</th><th>Requester</th><th>Institution</th>
    <th>Countries</th><th>Status</th><th></th>
  </tr>{{ rejected_rows | safe }}</table>
 </div>
</div>

<!-- PRECISE Catalogue Users -->
<div class="page wide" style="margin:0 auto;padding:0 24px 16px">
 <div class="card">
  <div class="logo" style="margin-bottom:4px">PRECISE Catalogue — Users</div>
  <div class="sub" style="margin-bottom:16px">placealert.org/catalogue/ &nbsp;·&nbsp; Per-user accounts &amp; token budgets</div>
  <div id="precUsersTable"><em style="color:#9ca3af">Loading…</em></div>
 </div>
</div>

<!-- PRECISE Catalogue Access Requests -->
<div class="page wide" style="margin:0 auto;padding:0 24px 16px">
 <div class="card">
  <div class="logo" style="margin-bottom:4px">PRECISE Catalogue — Access Requests</div>
  <h3 style="margin:12px 0 8px;color:#1a535c">Pending <span class="badge badge-pending">{{ prec_pending|length }}</span></h3>
  <table><tr><th>Date</th><th>Requester</th><th>Institution</th><th>Status</th><th>Actions</th></tr>{{ prec_pending_rows | safe }}</table>
  <h3 style="margin:20px 0 8px;color:#1a535c">Approved <span class="badge badge-approved">{{ prec_approved|length }}</span></h3>
  <table><tr><th>Date</th><th>Requester</th><th>Institution</th><th>Status</th><th></th></tr>{{ prec_approved_rows | safe }}</table>
  <h3 style="margin:20px 0 8px;color:#1a535c">Rejected <span class="badge badge-rejected">{{ prec_rejected|length }}</span></h3>
  <table><tr><th>Date</th><th>Requester</th><th>Institution</th><th>Status</th><th></th></tr>{{ prec_rejected_rows | safe }}</table>
 </div>
</div>

<!-- HE²AT Catalogue Users -->
<div class="page wide" style="margin:0 auto;padding:0 24px 16px">
 <div class="card">
  <div class="logo" style="margin-bottom:4px">HE²AT Catalogue — Users</div>
  <div class="sub" style="margin-bottom:16px">placealert.org/heat-catalogue/ &nbsp;·&nbsp; Per-user accounts &amp; token budgets</div>
  <div id="he2atUsersTable"><em style="color:#9ca3af">Loading…</em></div>
 </div>
</div>

<!-- HE²AT Catalogue Access Requests -->
<div class="page wide" style="margin:0 auto;padding:0 24px 32px">
 <div class="card">
  <div class="logo" style="margin-bottom:4px">HE²AT Catalogue — Access Requests</div>
  <h3 style="margin:12px 0 8px;color:#1a535c">Pending <span class="badge badge-pending">{{ he2at_pending|length }}</span></h3>
  <table><tr><th>Date</th><th>Requester</th><th>Institution</th><th>Status</th><th>Actions</th></tr>{{ he2at_pending_rows | safe }}</table>
  <h3 style="margin:20px 0 8px;color:#1a535c">Approved <span class="badge badge-approved">{{ he2at_approved|length }}</span></h3>
  <table><tr><th>Date</th><th>Requester</th><th>Institution</th><th>Status</th><th></th></tr>{{ he2at_approved_rows | safe }}</table>
  <h3 style="margin:20px 0 8px;color:#1a535c">Rejected <span class="badge badge-rejected">{{ he2at_rejected|length }}</span></h3>
  <table><tr><th>Date</th><th>Requester</th><th>Institution</th><th>Status</th><th></th></tr>{{ he2at_rejected_rows | safe }}</table>
 </div>
</div>

<!-- PALs Portal Users -->
<div class="page wide" style="margin:0 auto;padding:0 24px 32px">
 <div class="card">
  <div class="logo" style="margin-bottom:4px">PALs Portal — User Accounts</div>
  <div class="sub" style="margin-bottom:16px">portal.placealert.org &nbsp;·&nbsp; Self-service sign-ups with admin approval</div>
  <div id="portalUsersTable"><em style="color:#9ca3af">Loading…</em></div>
 </div>
</div>

<!-- Course Enrolments -->
<div class="page wide" style="margin:0 auto;padding:0 24px 32px">
 <div class="card">
  <div class="logo" style="margin-bottom:4px">Course Enrolments</div>
  <div class="sub" style="margin-bottom:16px">GeoAI for Climate, Environment &amp; Health — PALSlab Hub accounts created on approval &nbsp;&middot;&nbsp; <a href="https://pals.placealert.org/services/geoai-progress/" target="_blank" rel="noopener">View course progress dashboard &rarr;</a></div>
  <div class="sub" style="margin-bottom:16px">GeoAI: Air Quality Explorer — open course, tracked anonymously by session &nbsp;&middot;&nbsp; <a href="/duckrequest/admin/junior-course-progress" target="_blank" rel="noopener">View course progress dashboard &rarr;</a></div>
  <div id="courseEnrolTable"><em style="color:#9ca3af">Loading…</em></div>
 </div>
</div>

<!-- GeoAI Workshop — Token Budgets -->
<div class="page wide" style="margin:0 auto;padding:0 24px 32px">
 <div class="card">
  <div class="logo" style="margin-bottom:4px">GeoAI Workshop — Token Budgets</div>
  <div class="sub" style="margin-bottom:16px">pals.placealert.org &nbsp;·&nbsp; Per-student GeoAI token budgets (notebook 6)</div>
  <div id="workshopUsersTable"><em style="color:#9ca3af">Loading…</em></div>
 </div>
</div>

<!-- GeoAI Workshop — Survey Responses -->
<div class="page wide" style="margin:0 auto;padding:0 24px 32px">
 <div class="card">
  <div class="logo" style="margin-bottom:4px">GeoAI Workshop — Survey Responses</div>
  <div class="sub" style="margin-bottom:16px">Post-workshop feedback from participants</div>
  <div id="surveyResponsesTable"><em style="color:#9ca3af">Loading…</em></div>
 </div>
</div>

<!-- Course approve modal -->
<div class="modal-backdrop" id="courseApproveModal">
  <div class="modal">
    <h3>Approve Enrolment</h3>
    <p id="courseApproveFor" style="font-size:13px;color:#6b7280;margin-bottom:16px"></p>
    <p style="font-size:13px;color:#374151;margin-bottom:0">A JupyterHub account will be created using the password the student chose at signup, and they will be emailed their username.</p>
    <div class="modal-footer">
      <button class="btn" onclick="closeModals()" style="background:#e5e7eb;color:#374151">Cancel</button>
      <button class="btn btn-approve" onclick="submitCourseApprove()">Approve &amp; Create Account</button>
    </div>
  </div>
</div>

<!-- Course reject modal -->
<div class="modal-backdrop" id="courseRejectModal">
  <div class="modal">
    <h3>Reject Enrolment</h3>
    <p id="courseRejectFor" style="font-size:13px;color:#6b7280;margin-bottom:16px"></p>
    <label>Reason (optional, emailed to applicant)</label>
    <textarea id="courseRejectNotes" style="min-height:60px" placeholder="e.g. Workshop is now at capacity"></textarea>
    <div class="modal-footer">
      <button class="btn" onclick="closeModals()" style="background:#e5e7eb;color:#374151">Cancel</button>
      <button class="btn btn-reject" onclick="submitCourseReject()">Reject</button>
    </div>
  </div>
</div>

<!-- Course revoke modal -->
<div class="modal-backdrop" id="courseRevokeModal">
  <div class="modal">
    <h3 style="color:#7c3aed">Revoke Course Access</h3>
    <p id="courseRevokeFor" style="font-size:13px;color:#6b7280;margin-bottom:16px"></p>
    <div style="background:#faf5ff;border-left:3px solid #7c3aed;padding:10px 14px;
                border-radius:0 8px 8px 0;font-size:13px;color:#4c1d95;margin-bottom:16px">
      This will delete the student&#39;s PALSlab Hub account and remove them from the course roster.
      They will be notified by email.
    </div>
    <label>Reason (optional, emailed to student)</label>
    <textarea id="courseRevokeNotes" style="min-height:60px" placeholder="e.g. Participant no longer attending"></textarea>
    <div class="modal-footer">
      <button class="btn" onclick="closeModals()" style="background:#e5e7eb;color:#374151">Cancel</button>
      <button class="btn" style="background:#7c3aed;color:white" onclick="submitCourseRevoke()">Revoke Access</button>
    </div>
  </div>
</div>

<!-- PALSlab Hub Accounts -->
<div class="page wide" style="margin:0 auto;padding:0 24px 32px">
 <div class="card">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:4px">
   <div class="logo" style="margin-bottom:0">PALSlab Hub — Accounts</div>
   <button class="btn btn-approve btn-sm" onclick="document.getElementById('hubCreateModal').classList.add('open')"
     style="font-size:12px;padding:4px 12px">+ Create Account</button>
  </div>
  <div class="sub" style="margin-bottom:16px">pals.placealert.org &nbsp;·&nbsp; JupyterHub accounts (approve / reject / create)</div>
  <div id="hubUsersTable"><em style="color:#9ca3af">Loading…</em></div>
 </div>
</div>

<!-- PALs Earth Accounts -->
<div class="page wide" style="margin:0 auto;padding:0 24px 32px">
 <div class="card">
  <div class="logo" style="margin-bottom:4px">PALs Earth — Accounts</div>
  <div class="sub" style="margin-bottom:16px">portal.placealert.org/palsearth/ &nbsp;·&nbsp; pending sign-ups (approve / reject)</div>
  <div id="palsearthUsersTable"><em style="color:#9ca3af">Loading…</em></div>
 </div>
</div>

<!-- Approve modal -->
<div class="modal-backdrop" id="approveModal">
  <div class="modal">
    <h3>Approve Request</h3>
    <p id="approveFor" style="font-size:13px;color:#6b7280;margin-bottom:16px"></p>
    <div id="approveCountryFields">
      <label style="margin-top:0">Grant access to which countries?</label>
      <div class="check-group" style="margin-top:8px">
        <label><input type="checkbox" id="ck_Kenya" value="Kenya"/> Kenya</label>
        <label><input type="checkbox" id="ck_Mozambique" value="Mozambique"/> Mozambique</label>
        <label><input type="checkbox" id="ck_Gambia" value="Gambia"/> The Gambia</label>
      </div>
    </div>
    <p id="approveGeoNote" style="display:none;font-size:13px;color:#6b7280;margin:0 0 8px">
      Pan-African geodata API request \u2014 not country-restricted, no selection needed.
    </p>
    <label style="margin-top:16px">Notes (optional)</label>
    <textarea id="approveNotes" style="min-height:50px" placeholder="e.g. Approved for 12 months"></textarea>
    <div class="modal-footer">
      <button class="btn" onclick="closeModals()" style="background:#e5e7eb;color:#374151">Cancel</button>
      <button class="btn btn-approve" onclick="submitApprove()">Approve &amp; Send Key</button>
    </div>
  </div>
</div>

<!-- Reject modal -->
<div class="modal-backdrop" id="rejectModal">
  <div class="modal">
    <h3>Reject Request</h3>
    <p id="rejectFor" style="font-size:13px;color:#6b7280;margin-bottom:16px"></p>
    <label>Reason (optional, emailed to requester)</label>
    <textarea id="rejectNotes" style="min-height:60px" placeholder="e.g. Insufficient justification provided"></textarea>
    <div class="modal-footer">
      <button class="btn" onclick="closeModals()" style="background:#e5e7eb;color:#374151">Cancel</button>
      <button class="btn btn-reject" onclick="submitReject()">Reject Request</button>
    </div>
  </div>
</div>

<!-- Revoke modal -->
<div class="modal-backdrop" id="revokeModal">
  <div class="modal">
    <h3 style="color:#7c3aed">Revoke Access</h3>
    <p id="revokeFor" style="font-size:13px;color:#6b7280;margin-bottom:16px"></p>
    <div style="background:#faf5ff;border-left:3px solid #7c3aed;padding:10px 14px;
                border-radius:0 8px 8px 0;font-size:13px;color:#4c1d95;margin-bottom:16px">
      This immediately invalidates the user&#39;s API key.
      They will be notified by email and must re-apply for access.
    </div>
    <label>Reason (optional — emailed to user)</label>
    <textarea id="revokeNotes" style="min-height:60px" placeholder="e.g. Project period ended"></textarea>
    <div class="modal-footer">
      <button class="btn" onclick="closeModals()" style="background:#e5e7eb;color:#374151">Cancel</button>
      <button class="btn" style="background:#7c3aed;color:white" onclick="submitRevoke()">Revoke Access</button>
    </div>
  </div>
</div>

<script>
const PREFIX = '/duckrequest';
let _currentId = null;

let _currentIsGeo = false;

function openApprove(id, name, countries, isGeo) {
  _currentId = id;
  _currentIsGeo = !!isGeo;
  document.getElementById('approveFor').textContent = 'Requester: ' + name;
  document.getElementById('approveCountryFields').style.display = _currentIsGeo ? 'none' : '';
  document.getElementById('approveGeoNote').style.display = _currentIsGeo ? '' : 'none';
  if (!_currentIsGeo) {
    ['Kenya','Mozambique','Gambia'].forEach(c => {
      document.getElementById('ck_' + c).checked = countries.includes(c);
    });
  }
  document.getElementById('approveNotes').value = '';
  document.getElementById('approveModal').classList.add('open');
}

function openReject(id, name) {
  _currentId = id;
  document.getElementById('rejectFor').textContent = 'Requester: ' + name;
  document.getElementById('rejectNotes').value = '';
  document.getElementById('rejectModal').classList.add('open');
}

function openRevoke(id, name) {
  _currentId = id;
  document.getElementById('revokeFor').textContent =
    'Revoking access for: ' + name;
  document.getElementById('revokeNotes').value = '';
  document.getElementById('revokeModal').classList.add('open');
}

function closeModals() {
  document.querySelectorAll('.modal-backdrop').forEach(m => m.classList.remove('open'));
}

async function submitApprove() {
  let countries = [];
  if (!_currentIsGeo) {
    countries = ['Kenya','Mozambique','Gambia']
      .filter(c => document.getElementById('ck_' + c).checked);
    if (!countries.length) { alert('Select at least one country.'); return; }
  }
  const notes = document.getElementById('approveNotes').value.trim();
  const btn = document.querySelector('#approveModal .btn-approve');
  btn.disabled = true; btn.textContent = 'Sending…';
  try {
    const r = await fetch(PREFIX + '/admin/approve', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({request_id: _currentId, countries, notes})
    });
    const d = await r.json();
    if (d.ok) location.reload(); else { alert('Error: ' + d.error); btn.disabled=false; btn.textContent='Approve & Send Key'; }
  } catch(e) { alert('Network error'); btn.disabled=false; btn.textContent='Approve & Send Key'; }
}

async function submitReject() {
  const notes = document.getElementById('rejectNotes').value.trim();
  const r = await fetch(PREFIX + '/admin/reject', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({request_id: _currentId, notes})
  });
  const d = await r.json();
  if (d.ok) location.reload(); else alert('Error: ' + d.error);
}

async function submitRevoke() {
  const notes = document.getElementById('revokeNotes').value.trim();
  const r = await fetch(PREFIX + '/admin/revoke', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({request_id: _currentId, notes})
  });
  const d = await r.json();
  if (d.ok) location.reload(); else alert('Error: ' + d.error);
}

/* ── Catalogue user management ── */
function fmtTokens(n) {
  if (n >= 1e6) return (n/1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n/1e3).toFixed(0) + 'k';
  return String(n);
}

function renderUsersTable(users, elId) {
  const el = document.getElementById(elId);
  if (!el) return;
  if (!users.length) { el.innerHTML='<p style="color:#9ca3af;font-size:13px">No users yet.</p>'; return; }
  el.innerHTML = '<table><tr><th>User</th><th>Used</th><th>Budget</th><th>%</th><th>Last active</th><th>Status</th><th>Actions</th></tr>'
    + users.map(u => {
      const st = u.status || (u.is_active ? 'approved' : 'revoked');
      const pct = u.token_budget > 0 ? Math.round(u.tokens_used/u.token_budget*100) : 0;
      const bar = `<div style="height:6px;background:#e5e7eb;border-radius:3px;width:80px;display:inline-block;vertical-align:middle"><div style="height:6px;background:${pct>90?'#ef4444':pct>70?'#f59e0b':'#10b981'};border-radius:3px;width:${Math.min(pct,100)}%"></div></div>`;
      let statusBadge, actions;
      if (st === 'pending') {
        statusBadge = '<span class="badge badge-pending">Pending</span>';
        actions = `<button class="btn btn-sm btn-approve" onclick="openCatUserApprove('${u.id}','${u.name.replace(/'/g,"\\'")}')">Approve</button>
                   <button class="btn btn-sm btn-reject" style="margin-left:4px" onclick="openCatUserReject('${u.id}','${u.name.replace(/'/g,"\\'")}')">Reject</button>`;
      } else if (st === 'approved' && u.is_active) {
        statusBadge = '<span class="badge badge-approved">Active</span>';
        actions = `<button class="btn btn-sm btn-approve" onclick="openBudgetEdit('${u.id}','${u.name.replace(/'/g,"\\'")}',${u.token_budget})">Edit budget</button>
                   <button class="btn btn-sm btn-reject" style="margin-left:4px" onclick="catUserRevoke('${u.id}','${u.name.replace(/'/g,"\\'")}')">Revoke</button>`;
      } else if (st === 'rejected') {
        statusBadge = '<span class="badge badge-rejected">Rejected</span>';
        actions = '';
      } else {
        statusBadge = '<span class="badge" style="background:#f3e8ff;color:#6b21a8">Revoked</span>';
        actions = `<button class="btn btn-sm" style="background:#6b7280;color:white" onclick="catUserReinstate('${u.id}','${u.name.replace(/'/g,"\\'")}')">Reinstate</button>`;
      }
      return `<tr>
        <td>${u.name}<br><small style="color:#6b7280">${u.email}</small></td>
        <td>${st==='pending'?'—':fmtTokens(u.tokens_used)}</td>
        <td>${st==='pending'?'—':fmtTokens(u.token_budget)}</td>
        <td>${st==='pending'?'—':bar+' '+pct+'%'}</td>
        <td><small>${u.last_used ? u.last_used.slice(0,10) : '—'}</small></td>
        <td>${statusBadge}</td>
        <td>${actions}</td>
      </tr>`;
    }).join('')
    + '</table>';
}

let _catUserId = null;
function openCatUserApprove(id, name) {
  _catUserId = id;
  document.getElementById('catUserApproveFor').textContent = name;
  document.getElementById('catUserApproveModal').classList.add('open');
}
function openCatUserReject(id, name) {
  _catUserId = id;
  document.getElementById('catUserRejectFor').textContent = name;
  document.getElementById('catUserRejectNotes').value = '';
  document.getElementById('catUserRejectModal').classList.add('open');
}
async function submitCatUserApprove() {
  const btn = document.querySelector('#catUserApproveModal .btn-approve');
  btn.disabled = true; btn.textContent = 'Approving…';
  const r = await fetch(PREFIX + '/admin/cat-user-approve', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({user_id: _catUserId})
  });
  const d = await r.json();
  if (d.ok) { closeModals(); loadCatUsers(); } else { alert('Error: ' + d.error); btn.disabled=false; btn.textContent='Approve'; }
}
async function submitCatUserReject() {
  const notes = document.getElementById('catUserRejectNotes').value.trim();
  const r = await fetch(PREFIX + '/admin/cat-user-reject', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({user_id: _catUserId, notes})
  });
  const d = await r.json();
  if (d.ok) { closeModals(); loadCatUsers(); } else alert('Error: ' + d.error);
}

async function loadCatUsers() {
  const r = await fetch(PREFIX + '/admin/cat-users');
  const users = await r.json();
  renderUsersTable(users.filter(u => u.catalogue === 'precise'), 'precUsersTable');
  renderUsersTable(users.filter(u => u.catalogue === 'he2at'),   'he2atUsersTable');
}

let _budgetUserId = null;
function openBudgetEdit(id, name, current) {
  _budgetUserId = id;
  document.getElementById('budgetFor').textContent = name;
  document.getElementById('budgetInput').value = current;
  document.getElementById('budgetModal').classList.add('open');
}
async function submitBudget() {
  const val = parseInt(document.getElementById('budgetInput').value);
  if (isNaN(val) || val < 0) { alert('Enter a valid number'); return; }
  const btn = document.querySelector('#budgetModal .btn-approve');
  btn.disabled = true; btn.textContent = 'Saving…';
  const r = await fetch(PREFIX + '/admin/cat-user-budget', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({user_id: _budgetUserId, token_budget: val})
  });
  const d = await r.json();
  if (d.ok) { closeModals(); loadCatUsers(); }
  else { alert('Error: ' + d.error); btn.disabled=false; btn.textContent='Save'; }
}
async function catUserRevoke(id, name) {
  if (!confirm('Revoke access for ' + name + '? They will be notified by email.')) return;
  const r = await fetch(PREFIX + '/admin/cat-user-revoke', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({user_id: id})
  });
  const d = await r.json();
  if (d.ok) loadCatUsers(); else alert('Error: ' + d.error);
}
async function catUserReinstate(id, name) {
  if (!confirm('Reinstate access for ' + name + '?')) return;
  const r = await fetch(PREFIX + '/admin/cat-user-reinstate', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({user_id: id})
  });
  const d = await r.json();
  if (d.ok) loadCatUsers(); else alert('Error: ' + d.error);
}

loadCatUsers();

/* ── GeoAI workshop token budgets ── */
function renderWorkshopUsersTable(users) {
  const el = document.getElementById('workshopUsersTable');
  if (!el) return;
  if (!users.length) { el.innerHTML='<p style="color:#9ca3af;font-size:13px">No students have used GeoAI yet.</p>'; return; }
  el.innerHTML = '<table><tr><th>Student (hub username)</th><th>Used</th><th>Budget</th><th>%</th><th>Last active</th><th>Actions</th></tr>'
    + users.map(u => {
      const pct = u.token_budget > 0 ? Math.round(u.tokens_used/u.token_budget*100) : 0;
      const bar = `<div style="height:6px;background:#e5e7eb;border-radius:3px;width:80px;display:inline-block;vertical-align:middle"><div style="height:6px;background:${pct>90?'#ef4444':pct>70?'#f59e0b':'#10b981'};border-radius:3px;width:${Math.min(pct,100)}%"></div></div>`;
      return `<tr>
        <td>${u.hub_username}</td>
        <td>${fmtTokens(u.tokens_used)}</td>
        <td>${fmtTokens(u.token_budget)}</td>
        <td>${bar} ${pct}%</td>
        <td><small>${u.last_used ? u.last_used.slice(0,10) : '—'}</small></td>
        <td><button class="btn btn-sm btn-approve" onclick="openWorkshopBudgetEdit('${u.hub_username.replace(/'/g,"\\'")}',${u.token_budget})">Edit budget</button></td>
      </tr>`;
    }).join('')
    + '</table>';
}

async function loadWorkshopUsers() {
  const r = await fetch(PREFIX + '/admin/workshop-users');
  const users = await r.json();
  renderWorkshopUsersTable(users);
}

let _workshopBudgetUsername = null;
function openWorkshopBudgetEdit(hub_username, current) {
  _workshopBudgetUsername = hub_username;
  document.getElementById('workshopBudgetFor').textContent = hub_username;
  document.getElementById('workshopBudgetInput').value = current;
  document.getElementById('workshopBudgetModal').classList.add('open');
}
async function submitWorkshopBudget() {
  const val = parseInt(document.getElementById('workshopBudgetInput').value);
  if (isNaN(val) || val < 0) { alert('Enter a valid number'); return; }
  const btn = document.querySelector('#workshopBudgetModal .btn-approve');
  btn.disabled = true; btn.textContent = 'Saving…';
  const r = await fetch(PREFIX + '/admin/workshop-user-budget', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({hub_username: _workshopBudgetUsername, token_budget: val})
  });
  const d = await r.json();
  if (d.ok) { closeModals(); loadWorkshopUsers(); }
  else { alert('Error: ' + d.error); btn.disabled=false; btn.textContent='Save'; }
}

loadWorkshopUsers();

/* ── Workshop survey responses ── */
async function loadSurveyResponses() {
  const el = document.getElementById('surveyResponsesTable');
  if (!el) return;
  const r = await fetch(PREFIX + '/admin/survey-responses');
  const data = await r.json();
  if (!data.length) { el.innerHTML='<p style="color:#9ca3af;font-size:13px">No survey responses yet.</p>'; return; }
  const stars = n => n ? '★'.repeat(n) + '☆'.repeat(5-n) : '—';
  const conf  = n => n ? ['','1 - Not at all','2','3 - Moderate','4','5 - Very confident'][n] : '—';
  el.innerHTML = '<table><tr><th>Date</th><th>Name</th><th>Background</th><th>Rating</th>'
    + '<th>Met expectations?</th><th>Modules done</th><th>Confidence pre→post</th><th>Next topic</th><th>Recommend</th><th>Highlight</th><th>Improve</th></tr>'
    + data.map(r => `<tr>
      <td><small>${r.submitted_at.slice(0,10)}</small></td>
      <td>${r.name||'—'}<br><small style="color:#6b7280">${r.email||''}</small></td>
      <td><small>${r.background||'—'}</small></td>
      <td style="color:#d97706">${stars(r.overall_rating)}</td>
      <td><small>${r.met_expectations||'—'}</small></td>
      <td><small>${(r.modules_done||[]).join(', ')||'—'}</small></td>
      <td><small>${conf(r.confidence_pre)} → ${conf(r.confidence_post)}</small></td>
      <td><small>${r.next_topic||'—'}</small></td>
      <td><small>${r.recommend||'—'}</small></td>
      <td style="max-width:160px;font-size:12px">${r.highlight||'—'}</td>
      <td style="max-width:160px;font-size:12px">${r.improve||'—'}</td>
    </tr>`).join('') + '</table>';
}
loadSurveyResponses();

/* ── Course enrollment management ── */
let _courseEnrolId = null;

function renderCourseEnrolTable(enrollments) {
  const el = document.getElementById('courseEnrolTable');
  if (!el) return;
  if (!enrollments.length) {
    el.innerHTML = '<p style="color:#9ca3af;font-size:13px">No enrolments yet.</p>';
    return;
  }
  const badge = s => ({
    pending:  '<span class="badge badge-pending">Pending</span>',
    approved: '<span class="badge badge-approved">Approved</span>',
    rejected: '<span class="badge badge-rejected">Rejected</span>',
    revoked:  '<span class="badge" style="background:#f3e8ff;color:#6b21a8">Revoked</span>',
  })[s] || s;
  const rowHtml = e => {
    const safeName = e.name.replace(/'/g,"\\'");
    const actions = e.status === 'pending'
      ? `<button class="btn btn-sm btn-approve" onclick="openCourseApprove('${e.id}','${safeName}')">Approve</button>
         <button class="btn btn-sm btn-reject" style="margin-left:4px" onclick="openCourseReject('${e.id}','${safeName}')">Reject</button>`
      : e.status === 'approved'
      ? `<button class="btn btn-sm" style="background:#7c3aed;color:white" onclick="openCourseRevoke('${e.id}','${safeName}')">Revoke</button>`
      : e.status === 'rejected'
      ? `<button class="btn btn-sm" style="background:#e5e7eb;color:#374151" onclick="undoCourseReject('${e.id}')">Undo rejection</button>`
      : e.status === 'revoked'
      ? `<button class="btn btn-sm" style="background:#e5e7eb;color:#374151" onclick="undoCourseReject('${e.id}')">Reopen</button>`
      : '';
    return `<tr>
      <td><small>${e.created_at.slice(0,10)}</small></td>
      <td>${e.name}<br><small style="color:#6b7280">${e.email}</small></td>
      <td><small>${e.institution||'—'}</small></td>
      <td><small>${e.country||'—'}</small></td>
      <td>${badge(e.status)}</td>
      <td><small style="font-family:monospace">${e.hub_username||'—'}</small></td>
      <td>${actions}</td></tr>`;
  };
  // Group enrolments into per-course sections
  const COURSES = [
    {id:'geoai2026',   label:'GeoAI for Climate, Environment & Health',            hint:'JupyterHub account',      accent:'#2563eb'},
    {id:'spectra2026', label:'SPECTRA — Personal Exposure & Trajectory Analysis',  hint:'SPECTRA app · login = email', accent:'#ea580c'},
    {id:'gipex2026',   label:'GIPEX — Geospatial Indicators for Environmental Exposure', hint:'GIPEX app · login = email', accent:'#06b6d4'},
  ];
  const groups = {};
  enrollments.forEach(e => { (groups[e.course_id] = groups[e.course_id] || []).push(e); });
  const known = new Set(COURSES.map(c => c.id));
  const order = COURSES.slice();
  Object.keys(groups).forEach(cid => { if (!known.has(cid)) order.push({id:cid, label:cid, hint:'', accent:'#6b7280'}); });
  let html = '';
  order.forEach(c => {
    const list = groups[c.id];
    if (!list || !list.length) return;
    const pend = list.filter(e => e.status === 'pending').length;
    const colLabel = c.id === 'geoai2026' ? 'Hub user' : 'App login';
    html += `<div style="margin:22px 0 6px;display:flex;align-items:center;gap:8px;border-top:1px solid #eef1f4;padding-top:16px">
        <span style="width:11px;height:11px;border-radius:50%;background:${c.accent};flex:none"></span>
        <strong style="font-size:14px">${c.label}</strong>
        <span style="color:#9ca3af;font-size:12px">${list.length} total${pend ? ` · <b style="color:#b45309">${pend} pending</b>` : ''}</span>
        ${c.hint ? `<span style="margin-left:auto;color:#9ca3af;font-size:11px">${c.hint}</span>` : ''}
      </div>`;
    html += '<table><tr><th>Date</th><th>Applicant</th><th>Institution</th><th>Country</th><th>Status</th><th>' + colLabel + '</th><th>Actions</th></tr>'
      + list.map(rowHtml).join('') + '</table>';
  });
  el.innerHTML = html;
}

async function loadCourseEnrolments() {
  const r = await fetch(PREFIX + '/admin/course-enrollments');
  const data = await r.json();
  renderCourseEnrolTable(Array.isArray(data) ? data : []);
}
loadCourseEnrolments();

function openCourseApprove(id, name) {
  _courseEnrolId = id;
  document.getElementById('courseApproveFor').textContent = 'Applicant: ' + name;
  document.getElementById('courseApproveModal').classList.add('open');
}
function openCourseReject(id, name) {
  _courseEnrolId = id;
  document.getElementById('courseRejectFor').textContent = 'Applicant: ' + name;
  document.getElementById('courseRejectNotes').value = '';
  document.getElementById('courseRejectModal').classList.add('open');
}
async function submitCourseApprove() {
  const btn = document.querySelector('#courseApproveModal .btn-approve');
  btn.disabled = true; btn.textContent = 'Creating account…';
  try {
    const r = await fetch(PREFIX + '/admin/course-approve', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({enrol_id: _courseEnrolId})
    });
    const d = await r.json();
    if (d.ok) { closeModals(); loadCourseEnrolments(); }
    else { alert('Error: ' + d.error); btn.disabled=false; btn.textContent='Approve & Create Account'; }
  } catch(e) { alert('Network error'); btn.disabled=false; btn.textContent='Approve & Create Account'; }
}
async function submitCourseReject() {
  const notes = document.getElementById('courseRejectNotes').value.trim();
  const r = await fetch(PREFIX + '/admin/course-reject', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({enrol_id: _courseEnrolId, notes})
  });
  const d = await r.json();
  if (d.ok) { closeModals(); loadCourseEnrolments(); }
  else alert('Error: ' + d.error);
}
async function undoCourseReject(id) {
  if (!confirm('Move this enrolment back to pending so you can reconsider it?')) return;
  const r = await fetch(PREFIX + '/admin/course-reopen', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({enrol_id: id})
  });
  const d = await r.json();
  if (d.ok) { loadCourseEnrolments(); }
  else alert('Error: ' + d.error);
}

let _courseRevokeId = null;
function openCourseRevoke(id, name) {
  _courseRevokeId = id;
  document.getElementById('courseRevokeFor').textContent = 'Student: ' + name;
  document.getElementById('courseRevokeNotes').value = '';
  document.getElementById('courseRevokeModal').classList.add('open');
}
async function submitCourseRevoke() {
  const notes = document.getElementById('courseRevokeNotes').value.trim();
  const r = await fetch(PREFIX + '/admin/course-revoke', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({enrol_id: _courseRevokeId, notes})
  });
  const d = await r.json();
  if (d.ok) { closeModals(); loadCourseEnrolments(); }
  else alert('Error: ' + d.error);
}

async function submitCatApprove() {
  const notes = document.getElementById('catApproveNotes').value.trim();
  const token_budget = parseInt(document.getElementById('catApproveBudget').value) || 1000000;
  const btn = document.querySelector('#catApproveModal .btn-approve');
  btn.disabled = true; btn.textContent = 'Sending…';
  try {
    const r = await fetch(PREFIX + '/admin/cat-approve', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({request_id: _currentId, notes, token_budget})
    });
    const d = await r.json();
    if (d.ok) location.reload(); else { alert('Error: ' + d.error); btn.disabled=false; btn.textContent='Approve & Send Credentials'; }
  } catch(e) { alert('Network error'); btn.disabled=false; btn.textContent='Approve & Send Credentials'; }
}

async function submitCatReject() {
  const notes = document.getElementById('catRejectNotes').value.trim();
  const r = await fetch(PREFIX + '/admin/cat-reject', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({request_id: _currentId, notes})
  });
  const d = await r.json();
  if (d.ok) location.reload(); else alert('Error: ' + d.error);
}

function openCatApprove(id, name, catalogue) {
  _currentId = id;
  document.getElementById('catApproveFor').textContent = name + ' (' + catalogue + ')';
  document.getElementById('catApproveNotes').value = '';
  document.getElementById('catApproveModal').classList.add('open');
}

function openCatReject(id, name) {
  _currentId = id;
  document.getElementById('catRejectFor').textContent = name;
  document.getElementById('catRejectNotes').value = '';
  document.getElementById('catRejectModal').classList.add('open');
}

/* ── Portal user management ── */
function renderPortalUsersTable(users) {
  const el = document.getElementById('portalUsersTable');
  if (!el) return;
  if (!users.length) { el.innerHTML='<p style="color:#9ca3af;font-size:13px">No portal users yet.</p>'; return; }
  const statusBadge = s => ({
    pending:  '<span class="badge badge-pending">Pending</span>',
    approved: '<span class="badge badge-approved">Approved</span>',
    rejected: '<span class="badge badge-rejected">Rejected</span>',
    revoked:  '<span class="badge" style="background:#f3e8ff;color:#6b21a8">Revoked</span>',
  })[s] || s;
  el.innerHTML = '<table><tr><th>Date</th><th>User</th><th>Institution / Purpose</th><th>Status</th><th>Actions</th></tr>'
    + users.map(u => {
      const notes = u.notes || '';
      const inst = (notes.match(/institution=([^;]*)/) || [])[1] || '—';
      const purpose = (notes.match(/purpose=(.*)$/) || [])[1] || '';
      const actions = u.status === 'pending'
        ? `<button class="btn btn-sm btn-approve" onclick="openPortalApprove('${u.id}','${u.name.replace(/'/g,"\\'")}')">Approve</button>
           <button class="btn btn-sm btn-reject" style="margin-left:4px" onclick="openPortalReject('${u.id}','${u.name.replace(/'/g,"\\'")}')">Reject</button>`
        : u.status === 'approved'
        ? `<button class="btn btn-sm" style="background:#7c3aed;color:white;margin-left:4px" onclick="portalRevoke('${u.id}','${u.name.replace(/'/g,"\\'")}')">Revoke</button>`
        : u.status === 'revoked'
        ? `<button class="btn btn-sm" style="background:#6b7280;color:white" onclick="portalReinstate('${u.id}','${u.name.replace(/'/g,"\\'")}')">Reinstate</button>`
        : '';
      return `<tr>
        <td><small>${u.created_at.slice(0,10)}</small></td>
        <td>${u.name}<br><small style="color:#6b7280">${u.email}</small></td>
        <td><small>${inst}</small>${purpose ? `<br><small style="color:#9ca3af">${purpose.slice(0,80)}</small>` : ''}</td>
        <td>${statusBadge(u.status)}</td>
        <td>${actions}</td>
      </tr>`;
    }).join('')
    + '</table>';
}

async function loadPortalUsers() {
  const r = await fetch(PREFIX + '/admin/portal-users');
  const users = await r.json();
  renderPortalUsersTable(users);
}

let _portalUserId = null;
function openPortalApprove(id, name) {
  _portalUserId = id;
  document.getElementById('portalApproveFor').textContent = name;
  document.getElementById('portalApproveModal').classList.add('open');
}
function openPortalReject(id, name) {
  _portalUserId = id;
  document.getElementById('portalRejectFor').textContent = name;
  document.getElementById('portalRejectNotes').value = '';
  document.getElementById('portalRejectModal').classList.add('open');
}
async function submitPortalApprove() {
  const btn = document.querySelector('#portalApproveModal .btn-approve');
  btn.disabled = true; btn.textContent = 'Approving…';
  const r = await fetch(PREFIX + '/admin/portal-approve', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({user_id: _portalUserId})
  });
  const d = await r.json();
  if (d.ok) { closeModals(); loadPortalUsers(); } else { alert('Error: ' + d.error); btn.disabled=false; btn.textContent='Approve'; }
}
async function submitPortalReject() {
  const notes = document.getElementById('portalRejectNotes').value.trim();
  const r = await fetch(PREFIX + '/admin/portal-reject', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({user_id: _portalUserId, notes})
  });
  const d = await r.json();
  if (d.ok) { closeModals(); loadPortalUsers(); } else alert('Error: ' + d.error);
}
async function portalRevoke(id, name) {
  if (!confirm('Revoke portal access for ' + name + '?')) return;
  const r = await fetch(PREFIX + '/admin/portal-revoke', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({user_id: id})
  });
  const d = await r.json();
  if (d.ok) loadPortalUsers(); else alert('Error: ' + d.error);
}
async function portalReinstate(id, name) {
  if (!confirm('Reinstate portal access for ' + name + '?')) return;
  const r = await fetch(PREFIX + '/admin/portal-reinstate', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({user_id: id})
  });
  const d = await r.json();
  if (d.ok) loadPortalUsers(); else alert('Error: ' + d.error);
}

loadPortalUsers();

/* ── PALSlab Hub account management ── */
function renderHubUsersTable(users) {
  const el = document.getElementById('hubUsersTable');
  if (!el) return;
  if (users.error) {
    el.innerHTML = '<p style="color:#ef4444;font-size:13px">Cannot read hub database: ' + users.error + '<br>Run: <code>sudo chgrp jupyterhub /var/lib/palslab-hub/jupyterhub.sqlite && sudo chmod 664 /var/lib/palslab-hub/jupyterhub.sqlite</code></p>';
    return;
  }
  if (!users.length) { el.innerHTML='<p style="color:#9ca3af;font-size:13px">No hub users yet.</p>'; return; }
  el.innerHTML = '<table><tr><th>Username</th><th>Email</th><th>Created</th><th>Last active</th><th>Status</th><th>Actions</th></tr>'
    + users.map(u => {
      const isPending  = !u.is_authorized;
      const isAdmin    = u.admin;
      const badge = isPending
        ? '<span class="badge badge-pending">Pending</span>'
        : isAdmin
        ? '<span class="badge badge-approved">Admin</span>'
        : '<span class="badge badge-approved">Active</span>';
      const actions = isPending
        ? `<button class="btn btn-sm btn-approve" onclick="hubApprove('${u.username}')">Approve</button>
           <button class="btn btn-sm btn-reject" style="margin-left:4px" onclick="openHubReject('${u.username}')">Reject</button>`
        : isAdmin ? '—'
        : `<button class="btn btn-sm btn-reject" onclick="openHubReject('${u.username}')" style="background:#7c3aed;color:white">Remove</button>`;
      return `<tr>
        <td><strong>${u.username}</strong></td>
        <td><small>${u.email || '—'}</small></td>
        <td><small>${u.created ? u.created.slice(0,10) : '—'}</small></td>
        <td><small>${u.last_activity ? u.last_activity.slice(0,10) : '—'}</small></td>
        <td>${badge}</td>
        <td>${actions}</td>
      </tr>`;
    }).join('') + '</table>';
}

async function loadHubUsers() {
  try {
    const r = await fetch(PREFIX + '/admin/hub-users');
    const data = await r.json();
    renderHubUsersTable(data);
  } catch(e) {
    const el = document.getElementById('hubUsersTable');
    if (el) el.innerHTML = '<p style="color:#ef4444;font-size:13px">Failed to load: ' + e + '</p>';
  }
}

async function hubApprove(username) {
  if (!confirm('Approve hub account for ' + username + '? They will be notified by email.')) return;
  const r = await fetch(PREFIX + '/admin/hub-approve', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username})
  });
  const d = await r.json();
  if (d.ok) loadHubUsers(); else alert('Error: ' + d.error);
}

let _hubRejectUser = null;
function openHubReject(username) {
  _hubRejectUser = username;
  document.getElementById('hubRejectFor').textContent = username;
  document.getElementById('hubRejectNotes').value = '';
  document.getElementById('hubRejectModal').classList.add('open');
}
async function submitHubReject() {
  const notes = document.getElementById('hubRejectNotes').value.trim();
  const r = await fetch(PREFIX + '/admin/hub-reject', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username: _hubRejectUser, notes})
  });
  const d = await r.json();
  if (d.ok) { closeModals(); loadHubUsers(); } else alert('Error: ' + d.error);
}

async function submitHubCreate() {
  const username = document.getElementById('hubCreateUsername').value.trim();
  const email    = document.getElementById('hubCreateEmail').value.trim();
  const password = document.getElementById('hubCreatePassword').value.trim();
  const notify   = document.getElementById('hubCreateNotify').checked;
  if (!username || !email || !password) { alert('All fields are required'); return; }
  const btn = document.querySelector('#hubCreateModal .btn-approve');
  btn.disabled = true; btn.textContent = 'Creating…';
  const r = await fetch(PREFIX + '/admin/hub-create', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username, email, password, notify})
  });
  const d = await r.json();
  if (d.ok) {
    closeModals();
    document.getElementById('hubCreateUsername').value = '';
    document.getElementById('hubCreateEmail').value = '';
    document.getElementById('hubCreatePassword').value = '';
    loadHubUsers();
  } else { alert('Error: ' + d.error); }
  btn.disabled = false; btn.textContent = 'Create Account';
}

loadHubUsers();

/* ── PALs Earth account management ── */
function renderPalsearthUsersTable(users) {
  const el = document.getElementById('palsearthUsersTable');
  if (!el) return;
  if (users.error) {
    el.innerHTML = '<p style="color:#ef4444;font-size:13px">Cannot read PALs Earth database: ' + users.error + '</p>';
    return;
  }
  if (!users.length) { el.innerHTML='<p style="color:#9ca3af;font-size:13px">No pending sign-ups.</p>'; return; }
  el.innerHTML = '<table><tr><th>Username</th><th>Email</th><th>Signed up</th><th>Actions</th></tr>'
    + users.map(u => `<tr>
        <td><strong>${u.username}</strong></td>
        <td><small>${u.email || '—'}</small></td>
        <td><small>${u.created_at ? u.created_at.slice(0,10) : '—'}</small></td>
        <td>
          <button class="btn btn-sm btn-approve" onclick="palsearthApprove('${u.username.replace(/'/g,"\\'")}')">Approve</button>
          <button class="btn btn-sm btn-reject" style="margin-left:4px" onclick="openPalsearthReject('${u.username.replace(/'/g,"\\'")}')">Reject</button>
        </td>
      </tr>`).join('') + '</table>';
}

async function loadPalsEarthUsers() {
  try {
    const r = await fetch(PREFIX + '/admin/palsearth-users');
    const data = await r.json();
    renderPalsearthUsersTable(data);
  } catch(e) {
    const el = document.getElementById('palsearthUsersTable');
    if (el) el.innerHTML = '<p style="color:#ef4444;font-size:13px">Failed to load: ' + e + '</p>';
  }
}

async function palsearthApprove(username) {
  if (!confirm('Approve PALs Earth account for ' + username + '? They will be notified by email.')) return;
  const r = await fetch(PREFIX + '/admin/palsearth-approve', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username})
  });
  const d = await r.json();
  if (d.ok) loadPalsEarthUsers(); else alert('Error: ' + d.error);
}

let _palsearthRejectUser = null;
function openPalsearthReject(username) {
  _palsearthRejectUser = username;
  document.getElementById('palsearthRejectFor').textContent = username;
  document.getElementById('palsearthRejectNotes').value = '';
  document.getElementById('palsearthRejectModal').classList.add('open');
}
async function submitPalsearthReject() {
  const notes = document.getElementById('palsearthRejectNotes').value.trim();
  const r = await fetch(PREFIX + '/admin/palsearth-reject', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({username: _palsearthRejectUser, notes})
  });
  const d = await r.json();
  if (d.ok) { closeModals(); loadPalsEarthUsers(); } else alert('Error: ' + d.error);
}

loadPalsEarthUsers();
</script>

<!-- Catalogue approve modal -->
<div class="modal-backdrop" id="catApproveModal">
  <div class="modal">
    <h3>Approve Catalogue Request</h3>
    <p id="catApproveFor" style="font-size:13px;color:#6b7280;margin-bottom:16px"></p>
    <p style="font-size:13px;color:#374151;margin-bottom:12px">An account will be created and credentials emailed automatically.</p>
    <label style="margin-top:0">Token budget (default 1,000,000)</label>
    <input id="catApproveBudget" type="number" value="1000000" min="0" step="100000" placeholder="1000000"/>
    <label style="margin-top:12px">Notes (optional, included in approval email)</label>
    <textarea id="catApproveNotes" style="min-height:50px" placeholder="e.g. Approved for 12 months"></textarea>
    <div class="modal-footer">
      <button class="btn" onclick="closeModals()" style="background:#e5e7eb;color:#374151">Cancel</button>
      <button class="btn btn-approve" onclick="submitCatApprove()">Approve &amp; Send Credentials</button>
    </div>
  </div>
</div>

<!-- Budget edit modal -->
<div class="modal-backdrop" id="budgetModal">
  <div class="modal">
    <h3>Edit Token Budget</h3>
    <p id="budgetFor" style="font-size:13px;color:#6b7280;margin-bottom:16px"></p>
    <label style="margin-top:0">New token budget</label>
    <input id="budgetInput" type="number" min="0" step="100000" placeholder="1000000"/>
    <p style="font-size:12px;color:#9ca3af;margin-top:6px">1,000,000 tokens ≈ 150–200 research questions</p>
    <div class="modal-footer">
      <button class="btn" onclick="closeModals()" style="background:#e5e7eb;color:#374151">Cancel</button>
      <button class="btn btn-approve" onclick="submitBudget()">Save</button>
    </div>
  </div>
</div>

<!-- Workshop budget edit modal -->
<div class="modal-backdrop" id="workshopBudgetModal">
  <div class="modal">
    <h3>Edit GeoAI Token Budget</h3>
    <p id="workshopBudgetFor" style="font-size:13px;color:#6b7280;margin-bottom:16px"></p>
    <label style="margin-top:0">New token budget</label>
    <input id="workshopBudgetInput" type="number" min="0" step="50000" placeholder="200000"/>
    <p style="font-size:12px;color:#9ca3af;margin-top:6px">Default for new students is 200,000 tokens.</p>
    <div class="modal-footer">
      <button class="btn" onclick="closeModals()" style="background:#e5e7eb;color:#374151">Cancel</button>
      <button class="btn btn-approve" onclick="submitWorkshopBudget()">Save</button>
    </div>
  </div>
</div>

<!-- Catalogue reject modal -->
<div class="modal-backdrop" id="catRejectModal">
  <div class="modal">
    <h3>Reject Catalogue Request</h3>
    <p id="catRejectFor" style="font-size:13px;color:#6b7280;margin-bottom:16px"></p>
    <label>Reason (optional, emailed to requester)</label>
    <textarea id="catRejectNotes" style="min-height:60px" placeholder="e.g. Insufficient justification"></textarea>
    <div class="modal-footer">
      <button class="btn" onclick="closeModals()" style="background:#e5e7eb;color:#374151">Cancel</button>
      <button class="btn btn-reject" onclick="submitCatReject()">Reject Request</button>
    </div>
  </div>
</div>

<!-- Catalogue self-signup approve modal -->
<div class="modal-backdrop" id="catUserApproveModal">
  <div class="modal">
    <h3>Approve Catalogue Account</h3>
    <p id="catUserApproveFor" style="font-size:13px;color:#6b7280;margin-bottom:16px"></p>
    <p style="font-size:13px;color:#374151;margin-bottom:0">The user set their own password. An approval email will be sent and they can log in immediately.</p>
    <div class="modal-footer">
      <button class="btn" onclick="closeModals()" style="background:#e5e7eb;color:#374151">Cancel</button>
      <button class="btn btn-approve" onclick="submitCatUserApprove()">Approve</button>
    </div>
  </div>
</div>

<!-- Catalogue self-signup reject modal -->
<div class="modal-backdrop" id="catUserRejectModal">
  <div class="modal">
    <h3>Reject Catalogue Account</h3>
    <p id="catUserRejectFor" style="font-size:13px;color:#6b7280;margin-bottom:16px"></p>
    <label>Reason (optional, emailed to requester)</label>
    <textarea id="catUserRejectNotes" style="min-height:60px" placeholder="e.g. Outside our research scope"></textarea>
    <div class="modal-footer">
      <button class="btn" onclick="closeModals()" style="background:#e5e7eb;color:#374151">Cancel</button>
      <button class="btn btn-reject" onclick="submitCatUserReject()">Reject</button>
    </div>
  </div>
</div>

<!-- Portal approve modal -->
<div class="modal-backdrop" id="portalApproveModal">
  <div class="modal">
    <h3>Approve Portal Account</h3>
    <p id="portalApproveFor" style="font-size:13px;color:#6b7280;margin-bottom:16px"></p>
    <p style="font-size:13px;color:#374151;margin-bottom:0">An approval email will be sent and the user can log in immediately.</p>
    <div class="modal-footer">
      <button class="btn" onclick="closeModals()" style="background:#e5e7eb;color:#374151">Cancel</button>
      <button class="btn btn-approve" onclick="submitPortalApprove()">Approve</button>
    </div>
  </div>
</div>

<!-- Portal reject modal -->
<div class="modal-backdrop" id="portalRejectModal">
  <div class="modal">
    <h3>Reject Portal Account</h3>
    <p id="portalRejectFor" style="font-size:13px;color:#6b7280;margin-bottom:16px"></p>
    <label>Reason (optional, emailed to requester)</label>
    <textarea id="portalRejectNotes" style="min-height:60px" placeholder="e.g. Outside our research scope"></textarea>
    <div class="modal-footer">
      <button class="btn" onclick="closeModals()" style="background:#e5e7eb;color:#374151">Cancel</button>
      <button class="btn btn-reject" onclick="submitPortalReject()">Reject</button>
    </div>
  </div>
</div>

<!-- Hub reject / remove modal -->
<div class="modal-backdrop" id="hubRejectModal">
  <div class="modal">
    <h3>Remove Hub Account</h3>
    <p id="hubRejectFor" style="font-size:13px;color:#6b7280;margin-bottom:16px"></p>
    <label>Reason (optional, emailed to user)</label>
    <textarea id="hubRejectNotes" style="min-height:60px" placeholder="e.g. Account not approved for this course"></textarea>
    <div class="modal-footer">
      <button class="btn" onclick="closeModals()" style="background:#e5e7eb;color:#374151">Cancel</button>
      <button class="btn btn-reject" onclick="submitHubReject()">Remove Account</button>
    </div>
  </div>
</div>

<!-- PALs Earth reject modal -->
<div class="modal-backdrop" id="palsearthRejectModal">
  <div class="modal">
    <h3>Reject PALs Earth Sign-up</h3>
    <p id="palsearthRejectFor" style="font-size:13px;color:#6b7280;margin-bottom:16px"></p>
    <label>Reason (optional, emailed to user)</label>
    <textarea id="palsearthRejectNotes" style="min-height:60px" placeholder="e.g. Not a recognised research affiliation"></textarea>
    <div class="modal-footer">
      <button class="btn" onclick="closeModals()" style="background:#e5e7eb;color:#374151">Cancel</button>
      <button class="btn btn-reject" onclick="submitPalsearthReject()">Reject Sign-up</button>
    </div>
  </div>
</div>

<!-- Hub create account modal -->
<div class="modal-backdrop" id="hubCreateModal">
  <div class="modal">
    <h3>Create Hub Account</h3>
    <p style="font-size:13px;color:#6b7280;margin-bottom:16px">Account is created pre-approved. Credentials are emailed to the user if notify is checked.</p>
    <label style="margin-top:0">Username</label>
    <input id="hubCreateUsername" type="text" placeholder="e.g. jmukamana" autocapitalize="off" autocorrect="off"/>
    <label style="margin-top:12px">Email</label>
    <input id="hubCreateEmail" type="email" placeholder="user@institution.org"/>
    <label style="margin-top:12px">Password</label>
    <input id="hubCreatePassword" type="text" placeholder="Temporary password"/>
    <label style="margin-top:12px;display:flex;align-items:center;gap:8px;font-weight:400">
      <input id="hubCreateNotify" type="checkbox" checked style="width:auto;margin:0"/> Email credentials to user
    </label>
    <div class="modal-footer">
      <button class="btn" onclick="closeModals()" style="background:#e5e7eb;color:#374151">Cancel</button>
      <button class="btn btn-approve" onclick="submitHubCreate()">Create Account</button>
    </div>
  </div>
</div>

<!-- DHS Explorer — Admin (additive, self-contained: calls the standalone
     dhs.placealert.org API directly over CORS; does not touch or share state
     with anything else on this dashboard) -->
<div class="page wide" style="margin:0 auto;padding:0 24px 32px">
 <div class="card">
  <div class="logo" style="margin-bottom:4px">DHS Explorer — Researcher Access</div>
  <div class="sub" style="margin-bottom:16px">dhs.placealert.org &nbsp;·&nbsp; Standalone DHS survey-data portal — sign-in below uses its own separate admin account</div>
  <div id="dhsAdminPane">
    <div id="dhsLoginBox" style="max-width:340px">
      <label style="margin-top:0">DHS admin username</label>
      <input id="dhsAdminUser" type="text" autocapitalize="off" autocorrect="off"/>
      <label style="margin-top:12px">Password</label>
      <input id="dhsAdminPass" type="password"/>
      <button class="btn btn-approve" style="margin-top:14px" onclick="dhsAdminLogin()">Sign in</button>
      <div id="dhsLoginMsg" style="font-size:13px;margin-top:8px;color:#ef4444"></div>
    </div>
    <div id="dhsLoggedInBox" style="display:none">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <span style="font-size:13px;color:#6b7280">Signed in to DHS Explorer admin</span>
        <span style="font-size:13px;color:#ef4444;cursor:pointer" onclick="dhsAdminLogout()">Sign out</span>
      </div>
      <h3 style="margin:12px 0 8px;color:#1a535c">Pending requests <span class="badge badge-pending" id="dhsPendingCount">0</span></h3>
      <table><tr><th>Date</th><th>Requester</th><th>Institution / purpose</th><th>Countries requested</th><th>Actions</th></tr>
      <tbody id="dhsPendingRows"><tr><td colspan="5" style="color:#9ca3af;text-align:center">Loading…</td></tr></tbody></table>

      <h3 style="margin:24px 0 8px;color:#1a535c">All accounts</h3>
      <table><tr><th>Requester</th><th>Status</th><th>Countries approved</th><th>Actions</th></tr>
      <tbody id="dhsAllUsersRows"></tbody></table>

      <h3 style="margin:24px 0 8px;color:#1a535c">Invite a researcher</h3>
      <div style="max-width:420px">
        <label style="margin-top:0">Email</label>
        <input id="dhsInviteEmail" type="email"/>
        <label style="margin-top:12px">Name</label>
        <input id="dhsInviteName" type="text"/>
        <label style="margin-top:12px">Countries (comma-separated ISO2 codes, e.g. ZW,KE)</label>
        <input id="dhsInviteCountries" type="text" placeholder="ZW,KE"/>
        <button class="btn btn-approve" style="margin-top:14px" onclick="dhsSendInvite()">Send invite</button>
        <div id="dhsInviteMsg" style="font-size:13px;margin-top:8px"></div>
      </div>
      <div style="margin-top:12px"><a href="https://dhs.placealert.org/admin/" target="_blank" rel="noopener" style="font-size:13px">Open full DHS Explorer admin console &rarr;</a></div>
    </div>
  </div>
 </div>
</div>

<script>
(function(){
  const DHS_API = 'https://dhs.placealert.org/api/dhs';
  let dhsToken = localStorage.getItem('dhs_admin_token_embed') || '';

  function dhsHeaders() { return {'Content-Type':'application/json','X-Admin-Token':dhsToken}; }

  window.dhsAdminLogin = async function() {
    const username = document.getElementById('dhsAdminUser').value.trim();
    const password = document.getElementById('dhsAdminPass').value;
    try {
      const res = await fetch(DHS_API + '/admin/login', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})});
      const data = await res.json();
      if (!data.ok) { document.getElementById('dhsLoginMsg').textContent = data.error || 'Login failed'; return; }
      dhsToken = data.token;
      localStorage.setItem('dhs_admin_token_embed', dhsToken);
      dhsShowLoggedIn();
    } catch (e) {
      document.getElementById('dhsLoginMsg').textContent = 'Could not reach dhs.placealert.org';
    }
  };

  window.dhsAdminLogout = function() {
    localStorage.removeItem('dhs_admin_token_embed');
    dhsToken = '';
    document.getElementById('dhsLoggedInBox').style.display = 'none';
    document.getElementById('dhsLoginBox').style.display = 'block';
  };

  async function dhsShowLoggedIn() {
    document.getElementById('dhsLoginBox').style.display = 'none';
    document.getElementById('dhsLoggedInBox').style.display = 'block';
    dhsLoadPending();
    dhsLoadAllUsers();
  }

  async function dhsLoadPending() {
    const res = await fetch(DHS_API + '/admin/users?status=pending', {headers: dhsHeaders()});
    if (res.status === 401) { dhsAdminLogout(); return; }
    const data = await res.json();
    const users = data.users || [];
    document.getElementById('dhsPendingCount').textContent = users.length;
    document.getElementById('dhsPendingRows').innerHTML = users.length ? users.map(u => `
      <tr>
        <td>${(u.created_at||'').slice(0,10)}</td>
        <td>${u.name}<br><small style="color:#6b7280">${u.email}</small></td>
        <td><small>${u.institution||''}${u.purpose?' — '+u.purpose:''}</small></td>
        <td>${(u.countries_allowed||[]).join(', ') || '—'}</td>
        <td>
          <button class="btn btn-approve btn-sm" onclick="dhsApprove('${u.id}','${(u.countries_allowed||[]).join(',')}')">Approve</button>
          <button class="btn btn-reject btn-sm" style="margin-left:6px" onclick="dhsReject('${u.id}')">Reject</button>
        </td>
      </tr>`).join('') : '<tr><td colspan="5" style="color:#9ca3af;text-align:center">None</td></tr>';
  }

  async function dhsLoadAllUsers() {
    const res = await fetch(DHS_API + '/admin/users', {headers: dhsHeaders()});
    const data = await res.json();
    const users = data.users || [];
    const badgeClass = s => ({pending:'badge-pending',approved:'badge-approved',rejected:'badge-rejected',revoked:'badge-rejected'}[s] || '');
    document.getElementById('dhsAllUsersRows').innerHTML = users.map(u => `
      <tr>
        <td>${u.name}<br><small style="color:#6b7280">${u.email}</small></td>
        <td><span class="badge ${badgeClass(u.status)}">${u.status}</span></td>
        <td><small>${(u.countries_allowed||[]).join(', ') || '—'}</small></td>
        <td>${u.status === 'approved' ? `<button class="btn btn-sm" style="background:#7c3aed;color:white" onclick="dhsRevoke('${u.id}')">Revoke</button>` : ''}</td>
      </tr>`).join('');
  }

  window.dhsApprove = async function(id, requested) {
    const countries = prompt('Countries to approve (comma-separated ISO2 codes):', requested || '');
    if (countries === null) return;
    await fetch(DHS_API + '/admin/users/' + id + '/approve', {method:'POST', headers: dhsHeaders(),
      body: JSON.stringify({countries: countries.split(',').map(s=>s.trim().toUpperCase()).filter(Boolean)})});
    dhsLoadPending(); dhsLoadAllUsers();
  };
  window.dhsReject = async function(id) {
    await fetch(DHS_API + '/admin/users/' + id + '/reject', {method:'POST', headers: dhsHeaders(), body: JSON.stringify({})});
    dhsLoadPending(); dhsLoadAllUsers();
  };
  window.dhsRevoke = async function(id) {
    await fetch(DHS_API + '/admin/users/' + id + '/revoke', {method:'POST', headers: dhsHeaders()});
    dhsLoadAllUsers();
  };
  window.dhsSendInvite = async function() {
    const email = document.getElementById('dhsInviteEmail').value.trim();
    const name = document.getElementById('dhsInviteName').value.trim();
    const countries = document.getElementById('dhsInviteCountries').value.split(',').map(s=>s.trim().toUpperCase()).filter(Boolean);
    const res = await fetch(DHS_API + '/admin/invites', {method:'POST', headers: dhsHeaders(), body: JSON.stringify({email,name,countries})});
    const data = await res.json();
    document.getElementById('dhsInviteMsg').innerHTML = data.ok ? ('Invite sent: <a href="'+data.claim_url+'" target="_blank">'+data.claim_url+'</a>') : (data.error||'Failed');
  };

  if (dhsToken) { dhsShowLoggedIn(); }
})();
</script>
''',
    pending=pending, approved=approved, rejected=rejected, revoked=revoked,
    pending_rows=rows(pending, 'pending'),
    approved_rows=rows(approved, 'approved'),
    rejected_rows=rows(rejected, 'other'),
    revoked_rows=rows(revoked, 'other'),
    prec_pending=prec_pending, prec_approved=prec_approved, prec_rejected=prec_rejected,
    prec_pending_rows=cat_rows(prec_pending, 'pending'),
    prec_approved_rows=cat_rows(prec_approved, 'other'),
    prec_rejected_rows=cat_rows(prec_rejected, 'other'),
    he2at_pending=he2at_pending, he2at_approved=he2at_approved, he2at_rejected=he2at_rejected,
    he2at_pending_rows=cat_rows(he2at_pending, 'pending'),
    he2at_approved_rows=cat_rows(he2at_approved, 'other'),
    he2at_rejected_rows=cat_rows(he2at_rejected, 'other'),
    prefix=PREFIX,
    )


# ── Hub account management routes ────────────────────────────────────────────

@app.route('/admin/hub-users')
@admin_required
def admin_hub_users():
    users = hub_get_users()
    if isinstance(users, dict) and 'error' in users:
        return jsonify(users), 500
    course_usernames = access_db.get_course_student_hub_usernames()
    users = [u for u in users if u['username'] not in course_usernames]
    return jsonify(users)

@app.route('/admin/hub-approve', methods=['POST'])
@admin_required
def admin_hub_approve():
    d = request.get_json(force=True)
    username = d.get('username', '').strip()
    if not username:
        return jsonify({'ok': False, 'error': 'Missing username'})
    ok, err = hub_approve_user(username)
    if not ok:
        return jsonify({'ok': False, 'error': err})
    # Email the user
    users = hub_get_users()
    email = next((u['email'] for u in users if u['username'] == username), None)
    if email:
        _send(email, '[PALSlab Hub] Your account has been approved',
            f'Hi {username},\n\n'
            f'Your PALSlab Hub account has been approved.\n\n'
            f'Log in at: https://pals.placealert.org\n\n'
            f'— PALSlab Team')
    return jsonify({'ok': True})

@app.route('/admin/hub-reject', methods=['POST'])
@admin_required
def admin_hub_reject():
    d = request.get_json(force=True)
    username = d.get('username', '').strip()
    notes    = d.get('notes', '').strip()
    if not username:
        return jsonify({'ok': False, 'error': 'Missing username'})
    # Fetch email before deleting
    users = hub_get_users()
    user  = next((u for u in users if u['username'] == username), None)
    ok, err = hub_reject_user(username)
    if not ok:
        return jsonify({'ok': False, 'error': err})
    if user and user.get('email'):
        _send(user['email'], '[PALSlab Hub] Account request not approved',
            f'Hi {username},\n\n'
            f'Unfortunately your PALSlab Hub account request was not approved at this time.\n\n'
            + (f'Reason: {notes}\n\n' if notes else '')
            + f'If you believe this is an error, contact admin@example.org.\n\n'
              f'— PALSlab Team')
    return jsonify({'ok': True})

@app.route('/admin/hub-create', methods=['POST'])
@admin_required
def admin_hub_create():
    d = request.get_json(force=True)
    username = d.get('username', '').strip()
    email    = d.get('email', '').strip()
    password = d.get('password', '').strip()
    if not username or not email or not password:
        return jsonify({'ok': False, 'error': 'username, email and password are all required'})
    ok, err = hub_create_user(username, email, password)
    if not ok:
        return jsonify({'ok': False, 'error': err})
    notify = d.get('notify', True)
    if notify:
        _send(email, '[PALSlab Hub] Your account is ready',
            f'Hi {username},\n\n'
            f'Your PALSlab Hub account has been created.\n\n'
            f'  URL:      https://pals.placealert.org\n'
            f'  Username: {username}\n'
            f'  Password: {password}\n\n'
            f'Please change your password after first login.\n\n'
            f'— PALSlab Team')
    return jsonify({'ok': True})


@app.route('/admin/palsearth-users')
@admin_required
def admin_palsearth_users():
    users = palsearth_get_pending_users()
    if isinstance(users, dict) and 'error' in users:
        return jsonify(users), 500
    return jsonify(users)


@app.route('/admin/palsearth-approve', methods=['POST'])
@admin_required
def admin_palsearth_approve():
    d = request.get_json(force=True) or {}
    username = (d.get('username') or '').strip()
    if not username:
        return jsonify({'ok': False, 'error': 'Missing username'})
    user = palsearth_get_user(username)
    ok, err = palsearth_approve_user(username)
    if not ok:
        return jsonify({'ok': False, 'error': err or 'User not found'})
    if user and user.get('email'):
        _send(user['email'], '[PALs Earth] Your account has been approved',
            f'Hi {username},\n\n'
            f'Your PALs Earth account has been approved. You can now sign in and '
            f'start extracting data at:\n\n'
            f'https://portal.placealert.org/palsearth/\n\n'
            f'PALS Lab Team')
    return jsonify({'ok': True})


@app.route('/admin/palsearth-reject', methods=['POST'])
@admin_required
def admin_palsearth_reject():
    d = request.get_json(force=True) or {}
    username = (d.get('username') or '').strip()
    notes    = (d.get('notes') or '').strip()
    if not username:
        return jsonify({'ok': False, 'error': 'Missing username'})
    user = palsearth_get_user(username)
    ok, err = palsearth_deny_user(username)
    if not ok:
        return jsonify({'ok': False, 'error': err or 'User not found'})
    if user and user.get('email'):
        _send(user['email'], '[PALs Earth] Account request not approved',
            f'Hi {username},\n\n'
            f'Unfortunately your PALs Earth account request was not approved at this time.\n\n'
            + (f'Reason: {notes}\n\n' if notes else '')
            + f'Contact the administrator if you believe this is an error.\n\n'
              f'PALS Lab Team')
    return jsonify({'ok': True})


# ── DuckDB access management routes ──────────────────────────────────────────

@app.route('/admin/approve', methods=['POST'])
@admin_required
def admin_approve():
    d = request.get_json(force=True)
    req_id = d.get('request_id','')
    notes  = d.get('notes','')
    if not req_id:
        return jsonify({'ok': False, 'error': 'Missing request_id'})

    req = access_db.get_request(req_id)
    if not req:
        return jsonify({'ok': False, 'error': 'Request not found'})

    if (req.get('purpose') or '').startswith(GEODATA_TAG):
        key, req2 = access_db.approve_request(req_id, GEODATA_NO_COUNTRY_ACL, notes)
        if not key:
            return jsonify({'ok': False, 'error': 'Request not found'})
        notify_geodata_user_approved(req2['name'], req2['email'], key)
        return jsonify({'ok': True})

    countries = [c for c in d.get('countries',[]) if c in COUNTRIES]
    if not countries:
        return jsonify({'ok': False, 'error': 'Missing request_id or countries'})

    key, req2 = access_db.approve_request(req_id, countries, notes)
    if not key:
        return jsonify({'ok': False, 'error': 'Request not found'})

    notify_user_approved(req2['name'], req2['email'], key, countries)
    return jsonify({'ok': True})


@app.route('/admin/reject', methods=['POST'])
@admin_required
def admin_reject():
    d = request.get_json(force=True)
    req_id = d.get('request_id','')
    notes  = d.get('notes','')
    if not req_id:
        return jsonify({'ok': False, 'error': 'Missing request_id'})
    req = access_db.get_request(req_id)
    if not req:
        return jsonify({'ok': False, 'error': 'Request not found'})
    access_db.reject_request(req_id, notes)
    notify_user_rejected(req['name'], req['email'], notes)
    return jsonify({'ok': True})


@app.route('/admin/revoke', methods=['POST'])
@admin_required
def admin_revoke():
    d = request.get_json(force=True)
    req_id = d.get('request_id','')
    notes  = d.get('notes','')
    if not req_id:
        return jsonify({'ok': False, 'error': 'Missing request_id'})
    req = access_db.get_request(req_id)
    if not req:
        return jsonify({'ok': False, 'error': 'Request not found'})
    access_db.revoke_access(req_id)
    _send(
        req['email'],
        '[PRECISE DuckDB] Your access has been revoked',
        f'Hi {req["name"]},\n\n'
        f'Your access to the PRECISE DuckDB has been revoked.\n\n'
        + (f'Reason: {notes}\n\n' if notes else '')
        + f'Your API key is no longer valid. Any further queries will be rejected.\n\n'
          f'You may submit a new access request at:\n'
          f'https://placealert.org/duckrequest/request\n\n'
          f'PALS Lab Team'
    )
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════════════
# CATALOGUE ACCESS REQUESTS (PRECISE & HE2AT)
# ══════════════════════════════════════════════════════════════════════════════

CATALOGUE_LABELS = {'precise': 'PRECISE Catalogue', 'he2at': 'HE²AT Catalogue'}


# ── Portal signup notifications ───────────────────────────────────────────────

def notify_portal_approved(name, email):
    _send(
        email,
        '[PALs Portal] Your account has been approved',
        f'Hi {name},\n\n'
        f'Your PALs Portal account has been approved.\n\n'
        f'Sign in at: https://portal.placealert.org/\n\n'
        f'PALS Lab Team'
    )


def notify_portal_rejected(name, email, notes=''):
    _send(
        email,
        '[PALs Portal] Your account request was not approved',
        f'Hi {name},\n\n'
        f'Unfortunately your request for a PALs Portal account was not approved at this time.\n\n'
        + (f'Reason: {notes}\n\n' if notes else '')
        + 'You may contact the administrator if you have questions.\n\nPALS Lab Team'
    )


def notify_catalogue_admin(req_id, catalogue, name, email, institution, reason):
    label = CATALOGUE_LABELS.get(catalogue, catalogue)
    body  = (
        f'A new {label} access request requires your approval.\n\n'
        f'Name:        {name}\n'
        f'Email:       {email}\n'
        f'Institution: {institution}\n\n'
        f'Reason:\n{reason}\n\n'
        f'Review at: https://placealert.org/duckrequest/admin\n'
        f'Request ID: {req_id}'
    )
    for addr in ADMIN_EMAILS:
        _send(addr, f'[{label}] New access request from {name}', body)


def notify_catalogue_approved(catalogue, name, email, password, notes):
    label = CATALOGUE_LABELS.get(catalogue, catalogue)
    url   = ('https://placealert.org/catalogue/' if catalogue == 'precise'
             else 'https://placealert.org/heat-catalogue/')
    body  = (
        f'Hi {name},\n\n'
        f'Your request to access the {label} has been approved.\n\n'
        + (f'Notes from reviewer: {notes}\n\n' if notes else '')
        + f'Your login credentials:\n'
        f'  Email:    {email}\n'
        f'  Password: {password}\n\n'
        f'Visit the catalogue to log in:\n{url}\n\n'
        f'Keep these credentials private.\n\n'
        f'PALS Lab Team'
    )
    _send(email, f'[{label}] Your access request has been approved', body)


def notify_catalogue_rejected(catalogue, name, email, notes):
    label = CATALOGUE_LABELS.get(catalogue, catalogue)
    body  = (
        f'Hi {name},\n\n'
        f'Unfortunately your request to access the {label} '
        f'was not approved at this time.\n\n'
        + (f'Reason: {notes}\n\n' if notes else '')
        + f'You may reapply if your circumstances change.\n\n'
          f'PALS Lab Team'
    )
    _send(email, f'[{label}] Your access request was not approved', body)


@app.route('/cat-submit', methods=['POST', 'OPTIONS'])
def cat_submit():
    """Store a catalogue access request and notify admins."""
    if request.method == 'OPTIONS':
        resp = app.make_default_options_response()
        resp.headers['Access-Control-Allow-Origin']  = '*'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp
    d           = request.get_json(force=True) or {}
    catalogue   = d.get('catalogue', '').strip()
    name        = d.get('name', '').strip()
    email       = d.get('email', '').strip()
    institution = d.get('institution', '').strip()
    reason      = d.get('reason', '').strip()

    if catalogue not in ('precise', 'he2at') or not all([name, email]):
        return jsonify({'ok': False, 'error': 'Missing required fields'}), 400

    req_id = access_db.create_catalogue_request(catalogue, name, email, institution, reason)
    notify_catalogue_admin(req_id, catalogue, name, email, institution, reason)
    resp = jsonify({'ok': True, 'request_id': req_id})
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp


@app.route('/admin/cat-approve', methods=['POST'])
@admin_required
def admin_cat_approve():
    d             = request.get_json(force=True) or {}
    req_id        = d.get('request_id', '')
    notes         = d.get('notes', '')
    token_budget  = int(d.get('token_budget', 1_000_000))
    req           = access_db.approve_catalogue_request(req_id, notes)
    if not req:
        return jsonify({'ok': False, 'error': 'Request not found'})
    import secrets as _sec
    password = _sec.token_urlsafe(10)
    access_db.create_catalogue_user(
        req['catalogue'], req['name'], req['email'], password, token_budget, notes
    )
    notify_catalogue_approved(req['catalogue'], req['name'], req['email'], password, notes)
    return jsonify({'ok': True})


@app.route('/admin/cat-reject', methods=['POST'])
@admin_required
def admin_cat_reject():
    d      = request.get_json(force=True) or {}
    req_id = d.get('request_id', '')
    notes  = d.get('notes', '')
    req    = access_db.reject_catalogue_request(req_id, notes)
    if not req:
        return jsonify({'ok': False, 'error': 'Request not found'})
    notify_catalogue_rejected(req['catalogue'], req['name'], req['email'], notes)
    return jsonify({'ok': True})


@app.route('/admin/cat-users')
@admin_required
def admin_cat_users():
    """JSON list of all catalogue users — consumed by the admin dashboard."""
    users = access_db.get_catalogue_users()
    return jsonify(users)


@app.route('/admin/cat-user-budget', methods=['POST'])
@admin_required
def admin_cat_user_budget():
    d          = request.get_json(force=True) or {}
    user_id    = d.get('user_id', '')
    new_budget = int(d.get('token_budget', 0))
    if not user_id or new_budget < 0:
        return jsonify({'ok': False, 'error': 'Invalid parameters'})
    access_db.update_catalogue_user_budget(user_id, new_budget)
    return jsonify({'ok': True})


@app.route('/admin/workshop-users')
@admin_required
def admin_workshop_users():
    """JSON list of all GeoAI workshop students' token usage — consumed by the admin dashboard."""
    users = access_db.list_workshop_users()
    return jsonify(users)


@app.route('/admin/workshop-user-budget', methods=['POST'])
@admin_required
def admin_workshop_user_budget():
    d            = request.get_json(force=True) or {}
    hub_username = d.get('hub_username', '')
    new_budget   = int(d.get('token_budget', 0))
    if not hub_username or new_budget < 0:
        return jsonify({'ok': False, 'error': 'Invalid parameters'})
    access_db.update_workshop_user_budget(hub_username, new_budget)
    return jsonify({'ok': True})


@app.route('/admin/cat-user-revoke', methods=['POST'])
@admin_required
def admin_cat_user_revoke():
    d       = request.get_json(force=True) or {}
    user_id = d.get('user_id', '')
    if not user_id:
        return jsonify({'ok': False, 'error': 'Missing user_id'})
    access_db.revoke_catalogue_user(user_id)
    req = access_db.get_catalogue_user(user_id)
    if req:
        label = CATALOGUE_LABELS.get(req['catalogue'], req['catalogue'])
        _send(req['email'],
              f'[{label}] Your access has been revoked',
              f'Hi {req["name"]},\n\nYour access to the {label} has been revoked.\n'
              f'Contact the administrator if you believe this is an error.\n\nPALS Lab Team')
    return jsonify({'ok': True})


@app.route('/admin/cat-user-reinstate', methods=['POST'])
@admin_required
def admin_cat_user_reinstate():
    d       = request.get_json(force=True) or {}
    user_id = d.get('user_id', '')
    if not user_id:
        return jsonify({'ok': False, 'error': 'Missing user_id'})
    access_db.reinstate_catalogue_user(user_id)
    return jsonify({'ok': True})


# ── Catalogue self-signup approval routes ────────────────────────────────────

@app.route('/admin/cat-user-approve', methods=['POST'])
@admin_required
def admin_cat_user_approve_signup():
    d       = request.get_json(force=True) or {}
    user_id = d.get('user_id', '')
    if not user_id:
        return jsonify({'ok': False, 'error': 'Missing user_id'})
    user = access_db.approve_catalogue_signup_user(user_id)
    if not user:
        return jsonify({'ok': False, 'error': 'User not found'})
    label = CATALOGUE_LABELS.get(user['catalogue'], user['catalogue'])
    url   = ('https://portal.placealert.org/catalogue/' if user['catalogue'] == 'precise'
             else 'https://portal.placealert.org/heat-catalogue/')
    _send(user['email'],
          f'[{label}] Your account has been approved',
          f'Hi {user["name"]},\n\n'
          f'Your request to access the {label} has been approved.\n\n'
          f'Sign in with the password you chose during sign-up:\n{url}\n\n'
          f'PALS Lab Team')
    return jsonify({'ok': True})


@app.route('/admin/cat-user-reject', methods=['POST'])
@admin_required
def admin_cat_user_reject_signup():
    d       = request.get_json(force=True) or {}
    user_id = d.get('user_id', '')
    notes   = d.get('notes', '')
    if not user_id:
        return jsonify({'ok': False, 'error': 'Missing user_id'})
    user = access_db.reject_catalogue_signup_user(user_id)
    if not user:
        return jsonify({'ok': False, 'error': 'User not found'})
    notify_catalogue_rejected(user['catalogue'], user['name'], user['email'], notes)
    return jsonify({'ok': True})


# ── Portal user admin routes ──────────────────────────────────────────────────

@app.route('/admin/portal-users')
@admin_required
def admin_portal_users():
    users = access_db.get_portal_users()
    return jsonify(users)


@app.route('/admin/portal-approve', methods=['POST'])
@admin_required
def admin_portal_approve():
    d       = request.get_json(force=True) or {}
    user_id = d.get('user_id', '')
    if not user_id:
        return jsonify({'ok': False, 'error': 'Missing user_id'})
    user = access_db.approve_portal_user(user_id)
    if not user:
        return jsonify({'ok': False, 'error': 'User not found'})
    notify_portal_approved(user['name'], user['email'])
    return jsonify({'ok': True})


@app.route('/admin/portal-reject', methods=['POST'])
@admin_required
def admin_portal_reject():
    d       = request.get_json(force=True) or {}
    user_id = d.get('user_id', '')
    notes   = d.get('notes', '')
    if not user_id:
        return jsonify({'ok': False, 'error': 'Missing user_id'})
    user = access_db.reject_portal_user(user_id)
    if not user:
        return jsonify({'ok': False, 'error': 'User not found'})
    notify_portal_rejected(user['name'], user['email'], notes)
    return jsonify({'ok': True})


@app.route('/admin/portal-revoke', methods=['POST'])
@admin_required
def admin_portal_revoke():
    d       = request.get_json(force=True) or {}
    user_id = d.get('user_id', '')
    if not user_id:
        return jsonify({'ok': False, 'error': 'Missing user_id'})
    user = access_db.revoke_portal_user(user_id)
    if user:
        _send(user['email'],
              '[PALs Portal] Your access has been revoked',
              f'Hi {user["name"]},\n\nYour PALs Portal access has been revoked.\n'
              f'Contact the administrator if you believe this is an error.\n\nPALS Lab Team')
    return jsonify({'ok': True})


@app.route('/admin/portal-reinstate', methods=['POST'])
@admin_required
def admin_portal_reinstate():
    d       = request.get_json(force=True) or {}
    user_id = d.get('user_id', '')
    if not user_id:
        return jsonify({'ok': False, 'error': 'Missing user_id'})
    access_db.reinstate_portal_user(user_id)
    return jsonify({'ok': True})


# ── Course enrollment ─────────────────────────────────────────────────────────

COURSE_ROSTER_FILE = '/var/lib/palslab-hub/geoai_students.txt'
COURSE_LABELS = {
    'geoai2026':   'GeoAI for Climate, Environment & Health',
    'spectra2026': 'SPECTRA — Personal Exposure & Trajectory Analysis',
    'gipex2026':   'GIPEX — Geospatial Indicators for Environmental Exposure',
}
COURSE_HUB_URL = 'https://pals.placealert.org'
GEOAI_NOTEBOOKS_SRC = '/home/USER/PRECISE/workshop/release'
GEOAI_DATA_SRC      = '/home/USER/PRECISE/workshop/data'
GEOAI_UTILS_SRC     = '/home/USER/PRECISE/workshop/shared_utils.py'

# ── App-based courses (NOT JupyterHub) ────────────────────────────────────────
# These courses grant access to a standalone web app with its own users DB,
# rather than a JupyterHub account. On approval we write the applicant straight
# into that app's users table (already-approved) and email them the app link.
# The applicant logs in with their EMAIL and the password they chose at enrolment.
COURSE_APPS = {
    'spectra2026': {
        'db':   '/home/USER/shiny_app/spectra_users.sqlite',
        'hash': 'sha256',            # SPECTRA stores digest(pw, 'sha256')
        'gate': 'verified',          # verified=1 means active
        'url':  'https://placealert.org/apex/',
        'note': 'Open SPECTRA and sign in with your email and the password you chose.',
    },
    'gipex2026': {
        'db':   '/home/USER/grid_indicators_app/gipex_users.sqlite',
        'hash': 'werkzeug',          # GIPEX uses werkzeug password hashes
        'gate': 'status',            # status='approved' means active
        'url':  'https://placealert.org/gipex/',
        'note': 'Open GIPEX and sign in with your email and the password you chose.',
    },
}


def provision_app_user(course_id, enrollment):
    """Insert/activate an approved applicant in a standalone app's users DB.
    Returns (username, None) on success or (None, error_message)."""
    import sqlite3, hashlib
    cfg   = COURSE_APPS[course_id]
    email = (enrollment.get('email') or '').lower().strip()
    name  = enrollment.get('name') or ''
    org   = enrollment.get('institution') or ''
    pw    = enrollment.get('password') or ''
    if not (email and pw):
        return None, 'Missing email or password'
    if cfg['hash'] == 'sha256':
        pwhash = hashlib.sha256(pw.encode('utf-8')).hexdigest()
    else:
        from werkzeug.security import generate_password_hash
        pwhash = generate_password_hash(pw)
    try:
        con = sqlite3.connect(cfg['db'])
        cur = con.cursor()
        exists = cur.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone()
        if cfg['gate'] == 'verified':          # SPECTRA schema
            if exists:
                cur.execute("UPDATE users SET password_hash=?, full_name=?, organization=?, "
                            "verified=1, verification_code=NULL WHERE email=?",
                            (pwhash, name, org, email))
            else:
                cur.execute("INSERT INTO users (email,password_hash,full_name,organization,role,verified) "
                            "VALUES (?,?,?,?, 'user', 1)", (email, pwhash, name, org))
        else:                                  # GIPEX schema (status gate)
            if exists:
                cur.execute("UPDATE users SET password_hash=?, full_name=?, organization=?, "
                            "status='approved', reviewed_at=datetime('now') WHERE email=?",
                            (pwhash, name, org, email))
            else:
                cur.execute("INSERT INTO users (email,password_hash,full_name,organization,role,status) "
                            "VALUES (?,?,?,?, 'user', 'approved')", (email, pwhash, name, org))
        con.commit()
        con.close()
        return email, None
    except Exception as e:
        return None, str(e)


def _make_hub_username(name, email):
    """Derive a clean JupyterHub username from name/email; fall back to email prefix."""
    import re
    parts = name.strip().lower().split()
    if len(parts) >= 2:
        base = parts[0] + '_' + parts[-1][0]
    else:
        base = email.split('@')[0].lower()
    base = re.sub(r'[^a-z0-9_]', '_', base)[:20].strip('_')
    # Uniqueness check against hub DB
    try:
        with _hub_conn() as con:
            taken = {r[0] for r in con.execute("SELECT username FROM users_info").fetchall()}
    except Exception:
        taken = set()
    username = base
    i = 2
    while username in taken:
        username = f'{base}{i}'
        i += 1
    return username


def _add_to_course_roster(username):
    """Append username to the course roster file read by pre_spawn_hook.

    pre_spawn_hook uses this list to keep course students out of the 'pals'
    group and limit them to the GeoAI_Workshop materials only — see
    /etc/palslab-hub/jupyterhub_config.py."""
    try:
        os.makedirs(os.path.dirname(COURSE_ROSTER_FILE), exist_ok=True)
        existing = set()
        if os.path.exists(COURSE_ROSTER_FILE):
            with open(COURSE_ROSTER_FILE) as f:
                existing = {line.strip() for line in f if line.strip()}
        if username in existing:
            return
        with open(COURSE_ROSTER_FILE, 'a') as f:
            f.write(username + '\n')
    except Exception:
        pass


def _remove_from_course_roster(username):
    """Remove a username from the course roster file (on revocation)."""
    try:
        if not os.path.exists(COURSE_ROSTER_FILE):
            return
        with open(COURSE_ROSTER_FILE) as f:
            lines = [line.strip() for line in f if line.strip()]
        lines = [u for u in lines if u != username]
        with open(COURSE_ROSTER_FILE, 'w') as f:
            f.write('\n'.join(lines) + ('\n' if lines else ''))
    except Exception:
        pass


def notify_course_admin(enrol_id, name, email, institution, country, course_id):
    label = COURSE_LABELS.get(course_id, course_id)
    _send(
        ADMIN_EMAILS,
        f'[PALSlab] New course enrolment: {name}',
        f'New enrolment request for {label}\n\n'
        f'  Name        : {name}\n'
        f'  Email       : {email}\n'
        f'  Institution : {institution}\n'
        f'  Country     : {country}\n\n'
        f'Review at: https://portal.placealert.org/duckrequest/admin\n'
    )


def notify_course_approved(name, email, course_id, hub_username):
    label = COURSE_LABELS.get(course_id, course_id)
    if course_id in COURSE_APPS:
        # Standalone app course — link to the app, log in with email + chosen password
        cfg = COURSE_APPS[course_id]
        _send(
            email,
            f'[PALSlab] Your enrolment has been approved — {label}',
            f'Hi {name},\n\n'
            f'Your enrolment in {label} has been approved.\n\n'
            f'{cfg["note"]}\n\n'
            f'    {cfg["url"]}\n\n'
            f'  Username : {email}\n\n'
            f'(Use the password you chose when you enrolled.)\n\n'
            f'If you have any issues, reply to this email.\n\n'
            f'— PALSlab Team'
        )
        return
    _send(
        email,
        f'[PALSlab] Your enrolment has been approved — {label}',
        f'Hi {name},\n\n'
        f'Your enrolment in {label} has been approved.\n\n'
        f'Log in to PALSlab Hub using the password you chose when you enrolled:\n\n'
        f'    {COURSE_HUB_URL}\n\n'
        f'  Username : {hub_username}\n\n'
        f'Your notebooks are in the GeoAI_Workshop/ folder in your home directory.\n\n'
        f'If you have any issues, reply to this email.\n\n'
        f'— PALSlab Team'
    )


def notify_course_rejected(name, email, course_id, notes=''):
    label = COURSE_LABELS.get(course_id, course_id)
    reason_line = f'\nReason: {notes}\n' if notes else ''
    _send(
        email,
        f'[PALSlab] Course enrolment update — {label}',
        f'Hi {name},\n\n'
        f'Unfortunately we were unable to approve your enrolment in {label} at this time.{reason_line}\n'
        f'If you believe this is an error, please reply to this email.\n\n'
        f'— PALSlab Team'
    )


def notify_course_revoked(name, email, course_id, notes=''):
    label = COURSE_LABELS.get(course_id, course_id)
    reason_line = f'\nReason: {notes}\n' if notes else ''
    _send(
        email,
        f'[PALSlab] Course access revoked — {label}',
        f'Hi {name},\n\n'
        f'Your access to {label} on PALSlab Hub has been revoked by an administrator.{reason_line}\n'
        f'If you believe this is an error, please reply to this email.\n\n'
        f'— PALSlab Team'
    )


@app.route('/course-enrol', methods=['GET', 'POST'])
def course_enrol():
    if request.method == 'POST':
        d = request.get_json(force=True) or {}
        name        = (d.get('name') or '').strip()
        email       = (d.get('email') or '').strip().lower()
        institution = (d.get('institution') or '').strip()
        country     = (d.get('country') or '').strip()
        password    = d.get('password') or ''
        course_id   = (d.get('course_id') or 'geoai2026').strip()
        if not all([name, email, institution, country, password]):
            return jsonify({'ok': False, 'error': 'All fields are required.'}), 400
        if len(password) < 8:
            return jsonify({'ok': False, 'error': 'Password must be at least 8 characters.'}), 400
        enrol_id, conflict = access_db.create_course_enrollment(
            course_id, name, email, institution, country, password
        )
        if enrol_id is None:
            if conflict == 'pending':
                return jsonify({'ok': False, 'error': 'Your application is already pending review.'}), 409
            if conflict == 'approved':
                return jsonify({'ok': False, 'error': 'You are already enrolled. Check your email for login details.'}), 409
            return jsonify({'ok': False, 'error': 'This email has already been submitted.'}), 409
        notify_course_admin(enrol_id, name, email, institution, country, course_id)
        return jsonify({'ok': True, 'message': 'Application received — you will hear from us within 24 hours.'})
    # GET — redirect to website form
    return redirect('https://placealert.org/course-enrol.html')


@app.route('/course-status', methods=['POST'])
def course_status():
    d         = request.get_json(force=True) or {}
    email     = (d.get('email') or '').strip().lower()
    course_id = (d.get('course_id') or 'geoai2026').strip()
    if not email:
        return jsonify({'ok': False, 'error': 'Email required'}), 400
    record = access_db.get_course_enrollment_by_email(course_id, email)
    if not record:
        return jsonify({'ok': True, 'status': 'not_found'})
    return jsonify({
        'ok':          True,
        'status':      record['status'],
        'name':        record.get('name', ''),
        'hub_username': record.get('hub_username') or '',
    })


@app.route('/course-count')
def course_count():
    # Public, read-only headline count for website course cards — no PII exposed.
    course_id = (request.args.get('course_id') or 'geoai2026').strip()
    count = access_db.get_course_enrollment_count(course_id, status='approved')
    return jsonify({'ok': True, 'course_id': course_id, 'count': count})


@app.route('/admin/course-enrollments')
@admin_required
def admin_course_enrollments():
    return jsonify(access_db.get_course_enrollments())


@app.route('/admin/course-approve', methods=['POST'])
@admin_required
def admin_course_approve():
    d        = request.get_json(force=True) or {}
    enrol_id = d.get('enrol_id', '')
    if not enrol_id:
        return jsonify({'ok': False, 'error': 'Missing enrol_id'})
    enrollment = access_db.get_course_enrollment(enrol_id)
    if not enrollment:
        return jsonify({'ok': False, 'error': 'Enrollment not found'})
    if not enrollment.get('password'):
        return jsonify({'ok': False, 'error': 'No password on file for this enrolment — it may already be approved/rejected.'})

    course_id = enrollment['course_id']

    # ── App-based courses (SPECTRA / GIPEX): provision into the app's own users
    #    DB and email the app link — NO JupyterHub account. ──────────────────────
    if course_id in COURSE_APPS:
        username, err = provision_app_user(course_id, enrollment)
        if not username:
            return jsonify({'ok': False, 'error': f'App account provisioning failed: {err}'})
        # Mark approved (username slot holds their app login = email); clears password
        access_db.approve_course_enrollment(enrol_id, username)
        notify_course_approved(
            enrollment['name'], enrollment['email'], course_id, username
        )
        return jsonify({'ok': True, 'hub_username': username})

    # ── JupyterHub courses (GeoAI): existing flow ────────────────────────────────
    # Generate hub username (the student's own chosen password is used as-is)
    hub_username = _make_hub_username(enrollment['name'], enrollment['email'])

    # Create JupyterHub account
    ok, err = hub_create_user(hub_username, enrollment['email'], enrollment['password'])
    if not ok:
        return jsonify({'ok': False, 'error': f'Hub account creation failed: {err}'})

    # Add to course roster (triggers notebook provisioning on first spawn)
    _add_to_course_roster(hub_username)

    # Mark approved in DB (also clears the stored password)
    access_db.approve_course_enrollment(enrol_id, hub_username)

    # Email student
    notify_course_approved(
        enrollment['name'], enrollment['email'],
        enrollment['course_id'], hub_username
    )
    return jsonify({'ok': True, 'hub_username': hub_username})


@app.route('/admin/course-reject', methods=['POST'])
@admin_required
def admin_course_reject():
    d        = request.get_json(force=True) or {}
    enrol_id = d.get('enrol_id', '')
    notes    = d.get('notes', '')
    if not enrol_id:
        return jsonify({'ok': False, 'error': 'Missing enrol_id'})
    enrollment = access_db.get_course_enrollment(enrol_id)
    if not enrollment:
        return jsonify({'ok': False, 'error': 'Enrollment not found'})
    access_db.reject_course_enrollment(enrol_id, notes)
    notify_course_rejected(enrollment['name'], enrollment['email'], enrollment['course_id'], notes)
    return jsonify({'ok': True})


@app.route('/admin/course-reopen', methods=['POST'])
@admin_required
def admin_course_reopen():
    d        = request.get_json(force=True) or {}
    enrol_id = d.get('enrol_id', '')
    if not enrol_id:
        return jsonify({'ok': False, 'error': 'Missing enrol_id'})
    result = access_db.reopen_course_enrollment(enrol_id)
    if not result:
        return jsonify({'ok': False, 'error': 'Enrollment not found or not currently rejected'})
    return jsonify({'ok': True})


@app.route('/admin/course-revoke', methods=['POST'])
@admin_required
def admin_course_revoke():
    d        = request.get_json(force=True) or {}
    enrol_id = d.get('enrol_id', '')
    notes    = d.get('notes', '')
    if not enrol_id:
        return jsonify({'ok': False, 'error': 'Missing enrol_id'})
    result = access_db.revoke_course_enrollment(enrol_id)
    if not result:
        return jsonify({'ok': False, 'error': 'Enrollment not found or not currently approved'})
    hub_username = result.get('hub_username')
    if hub_username:
        hub_reject_user(hub_username)
        _remove_from_course_roster(hub_username)
    notify_course_revoked(result['name'], result['email'], result['course_id'], notes)
    return jsonify({'ok': True})


# ── Workshop survey ───────────────────────────────────────────────────────────

@app.route('/workshop-survey', methods=['POST'])
def workshop_survey():
    d = request.get_json(force=True) or {}
    modules_done    = [m for m in d.get('modules_done', []) if isinstance(m, str)]
    overall_rating  = d.get('overall_rating')
    confidence_pre  = d.get('confidence_pre')
    confidence_post = d.get('confidence_post')
    if overall_rating is not None:
        try:
            overall_rating = int(overall_rating)
            if overall_rating not in range(1, 6):
                overall_rating = None
        except (ValueError, TypeError):
            overall_rating = None
    for key, val in [('confidence_pre', confidence_pre), ('confidence_post', confidence_post)]:
        if val is not None:
            try:
                v = int(val)
                if v not in range(1, 6):
                    v = None
            except (ValueError, TypeError):
                v = None
            if key == 'confidence_pre':
                confidence_pre = v
            else:
                confidence_post = v
    access_db.create_survey_response(
        name             = str(d.get('name', '')).strip()[:200] or None,
        email            = str(d.get('email', '')).strip()[:200] or None,
        background       = str(d.get('background', '')).strip()[:200] or None,
        overall_rating   = overall_rating,
        modules_done     = modules_done,
        confidence_pre   = confidence_pre,
        confidence_post  = confidence_post,
        met_expectations = str(d.get('met_expectations', '')).strip()[:200] or None,
        next_topic       = str(d.get('next_topic', '')).strip()[:200] or None,
        highlight        = str(d.get('highlight', '')).strip()[:2000] or None,
        improve          = str(d.get('improve', '')).strip()[:2000] or None,
        recommend        = str(d.get('recommend', '')).strip()[:100] or None,
    )
    return jsonify({'ok': True})


@app.route('/admin/survey-responses')
@admin_required
def admin_survey_responses():
    return jsonify(access_db.get_survey_responses())




@app.route('/junior-course-count')
def junior_course_count():
    course_id = request.args.get('course_id') or 'geoai-air-quality'
    rows = access_db.get_junior_progress(course_id)
    completed = sum(1 for r in rows if r.get('status') == 'completed')
    return jsonify({'ok': True, 'course_id': course_id, 'count': len(rows), 'completed': completed})


@app.route('/junior-course-progress', methods=['POST', 'OPTIONS'])
def junior_course_progress():
    if request.method == 'OPTIONS':
        return ('', 204)
    d = request.get_json(force=True) or {}
    session_id = str(d.get('session_id', '')).strip()[:64]
    if not session_id:
        return jsonify({'ok': False, 'error': 'missing session_id'}), 400
    course_id = str(d.get('course_id', 'geoai-air-quality')).strip()[:100] or 'geoai-air-quality'

    kwargs = {}
    for key in ('name', 'email', 'school'):
        v = d.get(key)
        if v is not None:
            v = str(v).strip()[:200]
            kwargs[key] = v or None
    for key in ('quiz_score', 'quiz_total', 'modules_done'):
        v = d.get(key)
        if v is not None:
            try:
                kwargs[key] = int(v)
            except (ValueError, TypeError):
                pass
    status = d.get('status')
    if status in ('started', 'completed'):
        kwargs['status'] = status

    access_db.create_or_update_junior_progress(course_id, session_id, **kwargs)
    return jsonify({'ok': True})


@app.route('/admin/junior-course-progress')
@admin_required
def admin_junior_course_progress():
    course_id = request.args.get('course_id') or None
    rows = access_db.get_junior_progress(course_id)

    if request.args.get('format') == 'csv':
        import csv, io
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(['name', 'email', 'school', 'course_id', 'quiz_score', 'quiz_total',
                          'modules_done', 'status', 'started_at', 'completed_at'])
        for r in rows:
            writer.writerow([r.get('name'), r.get('email'), r.get('school'), r.get('course_id'),
                              r.get('quiz_score'), r.get('quiz_total'), r.get('modules_done'),
                              r.get('status'), r.get('started_at'), r.get('completed_at')])
        from flask import Response
        return Response(buf.getvalue(), mimetype='text/csv',
                         headers={'Content-Disposition': 'attachment; filename=junior_course_progress.csv'})

    if request.args.get('format') == 'json':
        return jsonify(rows)

    total = len(rows)
    completed = sum(1 for r in rows if r.get('status') == 'completed')
    scored = [r['quiz_score'] for r in rows if r.get('quiz_score') is not None]
    avg_score = round(sum(scored) / len(scored), 2) if scored else None

    def esc(v):
        return '' if v is None else str(v)

    row_html = ''.join(
        f"<tr><td>{esc(r.get('name'))}</td><td>{esc(r.get('email'))}</td><td>{esc(r.get('school'))}</td>"
        f"<td>{esc(r.get('course_id'))}</td>"
        f"<td>{esc(r.get('quiz_score'))}/{esc(r.get('quiz_total'))}</td>"
        f"<td>{esc(r.get('modules_done'))}</td>"
        f"<td>{esc(r.get('status'))}</td>"
        f"<td>{esc(r.get('started_at'))[:19]}</td><td>{esc(r.get('completed_at'))[:19]}</td></tr>"
        for r in rows
    )
    html = f"""
    <html><head><title>GeoAI: Air Quality Explorer &mdash; Progress</title>
    <style>
      body{{font-family:-apple-system,Segoe UI,sans-serif;background:#F7F5F0;color:#1C2A3F;margin:0;padding:32px;}}
      h1{{font-size:20px;margin-bottom:4px;}}
      .stats{{color:#5C6B80;font-size:13px;margin-bottom:20px;}}
      table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px;}}
      th,td{{padding:8px 12px;border-bottom:1px solid rgba(18,33,59,0.12);text-align:left;}}
      th{{background:#12213B;color:#fff;position:sticky;top:0;}}
      a.export{{font-size:12px;color:#2F8F8C;font-weight:600;text-decoration:none;margin-left:12px;}}
    </style></head><body>
      <h1>GeoAI: Air Quality Explorer</h1>
      <div class="stats">{total} signed up &middot; {completed} completed &middot; average quiz score: {avg_score if avg_score is not None else '—'}
        <a class="export" href="?format=csv{'&course_id=' + course_id if course_id else ''}">Export CSV</a>
        <a class="export" href="?format=json{'&course_id=' + course_id if course_id else ''}">JSON</a>
        <a class="export" href="/duckrequest/admin/junior-quiz-attempts{'?course_id=' + course_id if course_id else ''}">View Quiz Attempts &rarr;</a>
      </div>
      <table>
        <thead><tr><th>Name</th><th>Email</th><th>School</th><th>Course</th><th>Quiz Score</th>
        <th>Modules Done</th><th>Status</th><th>Started</th><th>Completed</th></tr></thead>
        <tbody>{row_html}</tbody>
      </table>
    </body></html>
    """
    return html




@app.route('/junior-quiz-attempt', methods=['POST', 'OPTIONS'])
def junior_quiz_attempt():
    if request.method == 'OPTIONS':
        return ('', 204)
    d = request.get_json(force=True) or {}
    session_id = str(d.get('session_id', '')).strip()[:64]
    module_id = str(d.get('module_id', '')).strip()[:50]
    if not session_id or not module_id:
        return jsonify({'ok': False, 'error': 'missing session_id or module_id'}), 400
    course_id = str(d.get('course_id', 'geoai-air-quality')).strip()[:100] or 'geoai-air-quality'

    try:
        attempt_number = int(d.get('attempt_number', 1))
    except (ValueError, TypeError):
        attempt_number = 1

    def _to_int(v):
        try:
            return int(v) if v is not None else None
        except (ValueError, TypeError):
            return None

    score = _to_int(d.get('score'))
    total = _to_int(d.get('total'))
    passed = bool(d.get('passed'))
    answers = d.get('answers')
    if not isinstance(answers, list):
        answers = None

    access_db.create_quiz_attempt(course_id, session_id, module_id, attempt_number,
                                   score=score, total=total, passed=passed, answers=answers)
    return jsonify({'ok': True})


@app.route('/admin/junior-quiz-attempts')
@admin_required
def admin_junior_quiz_attempts():
    course_id = request.args.get('course_id') or 'geoai-air-quality'
    session_filter = request.args.get('session_id') or None
    rows = access_db.get_quiz_attempts(course_id, session_filter)

    if request.args.get('format') == 'json':
        return jsonify(rows)

    module_names = {'m1': 'Why It Matters', 'm2': 'Know Your Air', 'm3': 'Eyes in the Sky'}

    def esc(v):
        return '' if v is None else str(v)

    row_html = ''.join(
        f"<tr><td>{esc(r.get('session_id'))[:12]}&hellip;</td>"
        f"<td>{esc(module_names.get(r.get('module_id'), r.get('module_id')))}</td>"
        f"<td>{esc(r.get('attempt_number'))}</td>"
        f"<td>{esc(r.get('score'))}/{esc(r.get('total'))}</td>"
        f"<td>{'&#9989;' if r.get('passed') else '&mdash;'}</td>"
        f"<td>{esc(r.get('submitted_at'))[:19]}</td></tr>"
        for r in rows
    )
    total_attempts = len(rows)
    passed_attempts = sum(1 for r in rows if r.get('passed'))
    json_href = "?format=json" + (f"&session_id={session_filter}" if session_filter else '')
    html = f"""
    <html><head><title>GeoAI: Air Quality Explorer &mdash; Quiz Attempts</title>
    <style>
      body{{font-family:-apple-system,Segoe UI,sans-serif;background:#F7F5F0;color:#1C2A3F;margin:0;padding:32px;}}
      h1{{font-size:20px;margin-bottom:4px;}}
      .stats{{color:#5C6B80;font-size:13px;margin-bottom:20px;}}
      table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px;}}
      th,td{{padding:8px 12px;border-bottom:1px solid rgba(18,33,59,0.12);text-align:left;}}
      th{{background:#12213B;color:#fff;position:sticky;top:0;}}
      a.export{{font-size:12px;color:#2F8F8C;font-weight:600;text-decoration:none;margin-left:12px;}}
    </style></head><body>
      <h1>GeoAI: Air Quality Explorer &mdash; Quiz Attempts</h1>
      <div class="stats">{total_attempts} attempts logged &middot; {passed_attempts} passed
        <a class="export" href="/duckrequest/admin/junior-course-progress">&larr; Back to enrolments</a>
        <a class="export" href="{json_href}">JSON</a>
      </div>
      <table>
        <thead><tr><th>Session</th><th>Module</th><th>Attempt #</th><th>Score</th><th>Passed</th><th>Submitted</th></tr></thead>
        <tbody>{row_html}</tbody>
      </table>
    </body></html>
    """
    return html


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=35488, debug=False)
