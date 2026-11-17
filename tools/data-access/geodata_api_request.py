#!/usr/bin/env python3
"""
Roadnet / Euclidean Geodata API — Access Request Service — port 35490

  /request            user submits an access request
  /status/<id>        user checks their request status
  /admin              admin dashboard (login required) — scoped to this
                       service's own [GEODATA-API]-tagged requests
  /admin/login        admin login form
  /admin/approve      approve a request and issue an API key
  /admin/reject       reject a request

Deliberately NOT a clone of duckdb_request.py's ~2800-line service (PDF
generation, JupyterHub account management) — this only needs to hand out a
plain API key for the roadnet/euclidean geodata endpoints, which aren't
scoped to specific countries the way cohort-data access is. It shares the
same underlying access_db.py SQLite tables (requests/api_keys/admins) as
every other PALs request service, so any key issued here already works
against /precise-api/api/{roadnet,euclidean}/{query,admin} — no changes
needed there.
"""
import sys, os, smtplib
sys.path.insert(0, '/home/USER/PRECISE')
from functools import wraps
from email.message import EmailMessage

from flask import Flask, request, jsonify, render_template_string, redirect, url_for, session
import access_db

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or os.urandom(32)

TAG = '[GEODATA-API] '  # prefix on the shared `purpose` column so admin views here only see our own requests
NO_COUNTRY_ACL = ['(none — pan-African geodata API, not country-restricted)']

SMTP_FROM    = 'admin@example.org'
SMTP_PASS    = os.environ.get('SMTP_APP_PASSWORD', '')
ADMIN_EMAILS = ['admin@example.org']
PORTAL_URL   = 'https://portal.placealert.org'


def _send_email(to, subject, body):
    try:
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From']    = SMTP_FROM
        msg['To']      = to
        msg.set_content(body)
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(SMTP_FROM, SMTP_PASS)
            s.send_message(msg)
    except Exception:
        pass  # never let email failure break the response


def notify_admin(name, email, institution, purpose):
    body = (
        f"New Roadnet/Euclidean API access request\n\n"
        f"Name: {name}\nEmail: {email}\nInstitution: {institution or '(not given)'}\n"
        f"Intended use: {purpose}\n\n"
        f"Review: {PORTAL_URL}/georequest/admin"
    )
    for a in ADMIN_EMAILS:
        _send_email(a, f'[Geodata API] New request from {name}', body)


def notify_user_approved(name, email, api_key):
    body = (
        f"Hi {name},\n\n"
        f"Your Roadnet/Euclidean API key has been approved:\n\n"
        f"  {api_key}\n\n"
        f"Send it as an X-API-Key header, e.g.:\n\n"
        f"  curl -X POST {PORTAL_URL}/precise-api/api/roadnet/query \\\n"
        f"    -H \"X-API-Key: {api_key}\" \\\n"
        f"    -F 'geojson={{\"type\":\"Polygon\",\"coordinates\":[[...]]}}'\n\n"
        f"Full docs (endpoints, shapefile upload, admin-unit lookups) are on the map "
        f"pages themselves — click the \"API\" button:\n"
        f"  {PORTAL_URL}/roadnet/\n"
        f"  {PORTAL_URL}/euclidean/\n\n"
        f"— PALs"
    )
    _send_email(email, 'Your Roadnet/Euclidean API key', body)


def notify_user_rejected(name, email, notes):
    body = f"Hi {name},\n\nYour Roadnet/Euclidean API access request was not approved.\n"
    if notes:
        body += f"\nNotes: {notes}\n"
    body += "\nIf you have questions, get in touch via the portal's contact section.\n\n— PALs"
    _send_email(email, 'Your Roadnet/Euclidean API request', body)


# ── Shared page chrome (kept intentionally simple — this is a utility page, not a flagship UI) ──

_BASE_CSS = """
body { font-family: 'Source Sans 3', -apple-system, sans-serif; background: #14171a; color: #fffbf5;
       margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 2rem; }
.card { background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px;
        padding: 2rem 2.2rem; max-width: 480px; width: 100%; }
h1 { font-size: 1.15rem; margin: 0 0 0.3rem; }
.sub { font-family: monospace; font-size: 0.7rem; color: rgba(255,255,255,0.35); text-transform: uppercase;
       letter-spacing: 0.08em; margin-bottom: 1.3rem; }
label { display: block; font-size: 0.8rem; color: rgba(255,255,255,0.6); margin: 0.9rem 0 0.3rem; }
input, textarea { width: 100%; box-sizing: border-box; background: rgba(255,255,255,0.05);
        border: 1px solid rgba(255,255,255,0.12); color: #fffbf5; border-radius: 4px; padding: 0.5rem 0.6rem;
        font-size: 0.85rem; font-family: inherit; }
textarea { min-height: 70px; resize: vertical; }
input:focus, textarea:focus { outline: none; border-color: #B87333; }
button { margin-top: 1.3rem; width: 100%; background: #B87333; border: none; color: #14171a; font-weight: 600;
         padding: 0.65rem; border-radius: 4px; cursor: pointer; font-size: 0.85rem; }
button:hover { opacity: 0.9; }
.err { color: #ff8a80; font-size: 0.8rem; margin-top: 0.8rem; }
.ok { color: #4ECDC4; }
a { color: #4ECDC4; }
table { width: 100%; border-collapse: collapse; font-size: 0.8rem; margin-top: 1rem; }
th, td { text-align: left; padding: 0.5rem 0.6rem; border-bottom: 1px solid rgba(255,255,255,0.08); }
th { color: rgba(255,255,255,0.4); font-weight: 500; text-transform: uppercase; font-size: 0.65rem; letter-spacing: 0.05em; }
.badge { font-size: 0.65rem; padding: 0.15rem 0.5rem; border-radius: 3px; text-transform: uppercase; }
.badge-pending { background: rgba(184,115,51,0.2); color: #d99a5f; }
.badge-approved { background: rgba(78,205,196,0.15); color: #4ECDC4; }
.badge-rejected { background: rgba(255,138,128,0.15); color: #ff8a80; }
.actions button { width: auto; margin: 0 0.3rem 0 0; padding: 0.3rem 0.6rem; font-size: 0.72rem; }
.reject-btn { background: none; border: 1px solid rgba(255,138,128,0.4); color: #ff8a80; }
h2 { font-size: 0.95rem; margin: 1.6rem 0 0.4rem; color: rgba(255,255,255,0.5); }
.top { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 0.5rem; }
.top a { font-size: 0.75rem; }
"""

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>{{ title }}</title><style>""" + _BASE_CSS + """</style></head>
<body><div class="card">{{ body|safe }}</div></body></html>"""


def render(title, body_html, **ctx):
    return render_template_string(_PAGE, title=title, body=render_template_string(body_html, **ctx))


# ── Public request form ────────────────────────────────────────────────────────

_REQUEST_FORM = """
<h1>Roadnet &amp; Euclidean API Access</h1>
<div class="sub">Request an API key</div>
{% if submitted %}
  <p class="ok">Thanks — your request has been submitted for review. You'll get an email once it's
  approved (usually within a couple of days).</p>
  <p>Reference: <code>{{ req_id }}</code> — check status at
  <a href="/status/{{ req_id }}">/status/{{ req_id }}</a></p>
{% else %}
  <p style="font-size:0.85rem;color:rgba(255,255,255,0.6)">
  For querying population-weighted road density / road proximity for your own area
  programmatically — not required just to view the maps.</p>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="post" action="/submit">
    <label>Name</label><input name="name" required>
    <label>Email</label><input name="email" type="email" required>
    <label>Institution</label><input name="institution">
    <label>What will you use it for?</label>
    <textarea name="purpose" required placeholder="e.g. pollution-weighted road density for a study catchment in..."></textarea>
    <button type="submit">Submit request</button>
  </form>
{% endif %}
"""

@app.route('/', methods=['GET'])
@app.route('/request', methods=['GET'])
def request_form():
    return render('Roadnet & Euclidean API Access', _REQUEST_FORM, submitted=False, error=None, req_id=None)


def _wants_json():
    """True for the inline AJAX form embedded in the roadnet/euclidean API
    modals; false for someone submitting the standalone /request page
    directly (still supported as a fallback, just not linked anywhere)."""
    return 'application/json' in (request.headers.get('Accept') or '')


@app.route('/submit', methods=['POST'])
def submit():
    name        = (request.form.get('name') or '').strip()
    email       = (request.form.get('email') or '').strip()
    institution = (request.form.get('institution') or '').strip()
    purpose     = (request.form.get('purpose') or '').strip()

    if not name or not email or not purpose:
        error = 'Please fill in name, email, and intended use.'
        if _wants_json():
            return jsonify({'ok': False, 'error': error}), 400
        return render('Roadnet & Euclidean API Access', _REQUEST_FORM, submitted=False,
                       error=error, req_id=None)

    req_id = access_db.create_request(name, email, institution, '', TAG + purpose, NO_COUNTRY_ACL)
    notify_admin(name, email, institution, purpose)

    if _wants_json():
        return jsonify({'ok': True, 'req_id': req_id})
    return render('Roadnet & Euclidean API Access', _REQUEST_FORM, submitted=True, error=None, req_id=req_id)


_STATUS_PAGE = """
<h1>Request status</h1>
{% if found %}
  <p>Status: <span class="badge badge-{{ req.status }}">{{ req.status }}</span></p>
  <p style="font-size:0.8rem;color:rgba(255,255,255,0.5)">Submitted {{ req.created_at[:10] }}</p>
{% else %}
  <p class="err">No request found with that reference.</p>
{% endif %}
"""

@app.route('/status/<req_id>')
def status(req_id):
    req = access_db.get_request(req_id)
    found = bool(req and (req.get('purpose') or '').startswith(TAG))
    return render('Request status', _STATUS_PAGE, found=found, req=req)


# ── Admin ──────────────────────────────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    def decorated(*a, **kw):
        if not session.get('admin_user'):
            return redirect(url_for('admin_login'))
        return f(*a, **kw)
    return decorated


_ADMIN_LOGIN = """
<h1>Admin login</h1>
<div class="sub">Geodata API requests</div>
{% if error %}<div class="err">{{ error }}</div>{% endif %}
<form method="post">
  <label>Username</label><input name="username" required autofocus>
  <label>Password</label><input name="password" type="password" required>
  <button type="submit">Log in</button>
</form>
"""

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        ok, _must_change = access_db.check_admin(username, password)
        if ok:
            session['admin_user'] = username
            return redirect(url_for('admin_dashboard'))
        error = 'Invalid username or password.'
    return render('Admin login', _ADMIN_LOGIN, error=error)


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_user', None)
    return redirect(url_for('admin_login'))


_ADMIN_DASHBOARD = """
<div class="top"><h1>Geodata API requests</h1><a href="/admin/logout">Log out</a></div>
<div class="sub">Roadnet / Euclidean access — {{ pending|length }} pending</div>

<h2>Pending ({{ pending|length }})</h2>
<table>
<tr><th>Date</th><th>Name</th><th>Purpose</th><th></th></tr>
{% for r in pending %}
<tr>
  <td>{{ r.created_at[:10] }}</td>
  <td>{{ r.name }}<br><small style="color:rgba(255,255,255,0.4)">{{ r.email }}</small></td>
  <td>{{ r.purpose[tag_len:] }}</td>
  <td class="actions">
    <form method="post" action="/admin/approve" style="display:inline">
      <input type="hidden" name="request_id" value="{{ r.id }}">
      <button type="submit">Approve</button>
    </form>
    <form method="post" action="/admin/reject" style="display:inline">
      <input type="hidden" name="request_id" value="{{ r.id }}">
      <button type="submit" class="reject-btn">Reject</button>
    </form>
  </td>
</tr>
{% else %}
<tr><td colspan="4" style="text-align:center;color:rgba(255,255,255,0.3)">None</td></tr>
{% endfor %}
</table>

<h2>Approved ({{ approved|length }})</h2>
<table>
<tr><th>Date</th><th>Name</th><th>Purpose</th><th>Status</th></tr>
{% for r in approved %}
<tr>
  <td>{{ r.created_at[:10] }}</td>
  <td>{{ r.name }}<br><small style="color:rgba(255,255,255,0.4)">{{ r.email }}</small></td>
  <td>{{ r.purpose[tag_len:] }}</td>
  <td><span class="badge badge-approved">approved</span></td>
</tr>
{% else %}
<tr><td colspan="4" style="text-align:center;color:rgba(255,255,255,0.3)">None</td></tr>
{% endfor %}
</table>

<h2>Rejected ({{ rejected|length }})</h2>
<table>
<tr><th>Date</th><th>Name</th><th>Purpose</th><th>Status</th></tr>
{% for r in rejected %}
<tr>
  <td>{{ r.created_at[:10] }}</td>
  <td>{{ r.name }}</td>
  <td>{{ r.purpose[tag_len:] }}</td>
  <td><span class="badge badge-rejected">rejected</span></td>
</tr>
{% else %}
<tr><td colspan="4" style="text-align:center;color:rgba(255,255,255,0.3)">None</td></tr>
{% endfor %}
</table>
"""

@app.route('/admin')
@admin_required
def admin_dashboard():
    ours = [r for r in access_db.get_requests() if (r.get('purpose') or '').startswith(TAG)]
    pending  = [r for r in ours if r['status'] == 'pending']
    approved = [r for r in ours if r['status'] == 'approved']
    rejected = [r for r in ours if r['status'] == 'rejected']
    return render('Geodata API requests', _ADMIN_DASHBOARD, pending=pending, approved=approved,
                  rejected=rejected, tag_len=len(TAG))


@app.route('/admin/approve', methods=['POST'])
@admin_required
def admin_approve():
    req_id = request.form.get('request_id', '')
    req = access_db.get_request(req_id)
    if not req or not (req.get('purpose') or '').startswith(TAG):
        return redirect(url_for('admin_dashboard'))
    key, req2 = access_db.approve_request(req_id, NO_COUNTRY_ACL)
    if key:
        notify_user_approved(req2['name'], req2['email'], key)
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/reject', methods=['POST'])
@admin_required
def admin_reject():
    req_id = request.form.get('request_id', '')
    req = access_db.get_request(req_id)
    if not req or not (req.get('purpose') or '').startswith(TAG):
        return redirect(url_for('admin_dashboard'))
    access_db.reject_request(req_id, '')
    notify_user_rejected(req['name'], req['email'], '')
    return redirect(url_for('admin_dashboard'))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=35490, debug=False)
