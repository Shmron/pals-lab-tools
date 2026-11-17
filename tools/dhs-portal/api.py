"""
DHS Explorer — standalone private portal API.

New, separate deployment from ~/PRECISE/api.py (does not modify that file or
its catalogue). Mirrors the safety pattern proven by "Shmron" there:
server-enforced country scoping, whitelisted read-only SQL, ID stripping
before results reach the LLM, and full query audit logging — applied to the
pooled DHS (Demographic and Health Surveys) dataset in ~/PRECISE/dhs_*.duckdb.
"""
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import duckdb
import re
import io
import os
import csv as _csv
import json
import time
import hashlib
import threading
import smtplib
from email.message import EmailMessage
from collections import defaultdict

import dhs_access
import dhs_codebook

app = Flask(__name__)
CORS(app)

DHS_DB_DIR   = '/home/USER/PRECISE'
_QUERY_LOG   = '/home/USER/dhs_portal/logs/dhs_queries.jsonl'
PORTAL_URL   = 'https://dhs.placealert.org'
ADMIN_EMAILS = ['admin@example.org']
SMTP_FROM    = 'admin@example.org'
SMTP_PASS    = os.environ.get('SMTP_APP_PASSWORD', '')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')


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


def _log_query(session_token, user_id, tool, **kwargs):
    """Append one audit entry to the query log. Never raises."""
    try:
        os.makedirs(os.path.dirname(_QUERY_LOG), exist_ok=True)
        session_hash = hashlib.sha256(session_token.encode()).hexdigest()[:12] if session_token else 'anon'
        entry = {
            'ts':      time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'session': session_hash,
            'user_id': user_id,
            'tool':    tool,
            **kwargs,
        }
        with open(_QUERY_LOG, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception:
        pass


# ── Rate limiting (chat endpoint) ──────────────────────────────────────────────
_ip_hits: dict = defaultdict(list)
_RATE_WINDOW = 60
_RATE_LIMIT  = 30


def _allow_ip(ip: str) -> bool:
    now  = time.time()
    hits = [t for t in _ip_hits[ip] if now - t < _RATE_WINDOW]
    if len(hits) >= _RATE_LIMIT:
        _ip_hits[ip] = hits
        return False
    hits.append(now)
    _ip_hits[ip] = hits
    return True


# ══════════════════════════════════════════════════════════════════════════════
# DuckDB connection — attach every populated dhs_*.duckdb as its own schema,
# then create a view per table in the default schema so a query can just say
# "FROM women" / "FROM clusters" etc. All 13 table names are unique across
# recodes so there is no collision. Skips dhs_merged.duckdb (empty/abandoned).
# ══════════════════════════════════════════════════════════════════════════════

_base_conn = None
_base_conn_lock = threading.Lock()


def _init_base_conn():
    conn = duckdb.connect(':memory:')
    for code, (dbfile, table, _title, _desc) in dhs_codebook.RECODES.items():
        dbpath = os.path.join(DHS_DB_DIR, dbfile)
        if not os.path.exists(dbpath):
            continue
        alias = code.lower()
        conn.execute(f"ATTACH '{dbpath}' AS {alias} (READ_ONLY)")
        conn.execute(f'CREATE VIEW "{table}" AS SELECT * FROM {alias}."{table}"')
    return conn


def _open_conn():
    """Return a cursor on the shared, process-wide base connection instead of
    re-ATTACHing all 13 dhs_*.duckdb files (some multi-GB) on every tool call —
    ATTACH happens once per process lifetime. cursor() is DuckDB's documented
    way to get an independent, thread-safe handle onto an already-open
    connection; callers still call .execute()/.close() exactly as before."""
    global _base_conn
    if _base_conn is None:
        with _base_conn_lock:
            if _base_conn is None:
                _base_conn = _init_base_conn()
    return _base_conn.cursor()


_DHS_TABLES = [table for (_dbfile, table, _t, _d) in dhs_codebook.RECODES.values()]


# ── SQL safety ──────────────────────────────────────────────────────────────

def is_safe_query(sql):
    """Allow SELECT / WITH / SHOW / DESCRIBE / PRAGMA statements only."""
    stripped = sql.strip().upper()
    read_only_starts = ('SELECT', 'WITH', 'SHOW', 'DESCRIBE', 'DESC', 'PRAGMA', 'EXPLAIN')
    if not any(stripped.startswith(s) for s in read_only_starts):
        return False, "Only read-only queries are allowed (SELECT, SHOW, DESCRIBE)."
    forbidden = ['DROP', 'DELETE', 'INSERT', 'UPDATE', 'CREATE', 'ALTER', 'TRUNCATE', 'ATTACH', 'COPY']
    for word in forbidden:
        if re.search(r'\b' + word + r'\b', stripped):
            return False, f"Query contains forbidden keyword: {word}"
    return True, None


def _coerce(v):
    """Coerce DuckDB return values to JSON-serialisable Python scalars."""
    import datetime as _dt
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()
    if hasattr(v, '__float__') and not isinstance(v, (int, str, bool, type(None))):
        return float(v)
    return v


def _strip_id_cols(result):
    """Remove true respondent/household identifier columns from execute_query
    results before they are sent back to the LLM — see dhs_codebook.DHS_ID_COLS
    for exactly what's excluded and why v001/hv001 (cluster) is deliberately
    kept (it's the join key to GPS coordinates, not a respondent identifier)."""
    if 'columns' not in result or 'rows' not in result:
        return result
    id_cols = dhs_codebook.DHS_ID_COLS
    keep = [c for c in result['columns'] if c.lower() not in id_cols]
    if len(keep) == len(result['columns']):
        return result
    return {
        **result,
        'columns': keep,
        'rows': [{k: v for k, v in r.items() if k.lower() not in id_cols} for r in result['rows']],
    }


# Words that can legally follow a table reference without being a table alias
# — needed because "FROM births WHERE ..." must not have "WHERE" mistaken for
# an alias the same way "FROM births b" has "b" as one.
_SQL_RESERVED_AFTER_TABLE = frozenset({
    'WHERE', 'JOIN', 'INNER', 'LEFT', 'RIGHT', 'FULL', 'CROSS', 'NATURAL', 'ON',
    'USING', 'GROUP', 'ORDER', 'HAVING', 'LIMIT', 'UNION', 'INTERSECT', 'EXCEPT',
    'WINDOW', 'QUALIFY', 'AS',
})


def apply_dhs_country_filter(sql, countries):
    """Wrap every reference to a DHS table so queries are restricted to the
    caller's approved countries, server-side — mirrors ~/PRECISE/api.py's
    apply_country_filter(). Every DHS recode table carries country_code."""
    if not countries:
        return "SELECT 1 WHERE 1=0"
    c_list = ', '.join("'" + c.replace("'", "''") + "'" for c in countries)
    filtered = sql
    for table in _DHS_TABLES:
        # Match FROM/JOIN <table>, optionally followed by an alias (bare or
        # `AS alias`) — e.g. "FROM births b" or "FROM births AS b". Without
        # this, the model's own alias survives untouched after our wrapped
        # subquery gets its own "AS" clause, producing invalid double-aliased
        # SQL like `... AS "births" b`.
        pattern = re.compile(
            rf'\b(FROM|JOIN)\s+(?:"{table}"|{table})\b(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?',
            re.IGNORECASE,
        )

        def _sub(m, table=table):
            keyword = m.group(1)
            candidate = m.group(2)
            alias = candidate if (candidate and candidate.upper() not in _SQL_RESERVED_AFTER_TABLE) else table
            return f'{keyword} (SELECT * FROM "{table}" WHERE country_code IN ({c_list})) AS "{alias}"'

        filtered = pattern.sub(_sub, filtered)
    return filtered


# ══════════════════════════════════════════════════════════════════════════════
# Sessions
# ══════════════════════════════════════════════════════════════════════════════

def require_session():
    """Returns (user dict, None) or (None, error response)."""
    token = request.headers.get('X-Session-Token', '')
    user = dhs_access.get_user_from_token(token) if token else None
    if not user:
        return None, (jsonify({'error': 'Please sign in.'}), 401)
    return user, None


def require_admin():
    token = request.headers.get('X-Admin-Token', '')
    username = dhs_access.get_admin_from_token(token) if token else None
    if not username:
        return None, (jsonify({'error': 'Admin sign-in required.'}), 401)
    return username, None


# ══════════════════════════════════════════════════════════════════════════════
# Public: available countries/surveys (no auth — informs the signup form)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/dhs/countries')
def dhs_countries():
    conn = _open_conn()
    try:
        rows = conn.execute('''
            SELECT country_code, country_name, survey_code, dhs_phase, dhsyear,
                   COUNT(*) AS n_clusters
            FROM clusters
            GROUP BY 1, 2, 3, 4, 5
            ORDER BY country_name, dhsyear
        ''').fetchall()
    finally:
        conn.close()

    by_country = {}
    for country_code, country_name, survey_code, phase, year, n in rows:
        entry = by_country.setdefault(country_code, {
            'country_code': country_code, 'country_name': country_name, 'surveys': [],
        })
        entry['surveys'].append({
            'survey_code': survey_code, 'dhs_phase': phase,
            'dhsyear': int(year) if year else None, 'n_clusters': n,
        })
    return jsonify({'countries': sorted(by_country.values(), key=lambda d: d['country_name'] or '')})


# ══════════════════════════════════════════════════════════════════════════════
# Researcher signup / login / sessions
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/dhs/signup', methods=['POST'])
def dhs_signup():
    d           = request.json or {}
    name        = d.get('name', '').strip()
    email       = d.get('email', '').strip()
    password    = d.get('password', '')
    institution = d.get('institution', '').strip()
    purpose     = d.get('purpose', '').strip()
    countries   = d.get('countries_requested', [])

    if not name or not email or not password:
        return jsonify({'ok': False, 'error': 'Name, email and password are required'}), 400
    if len(password) < 8:
        return jsonify({'ok': False, 'error': 'Password must be at least 8 characters'}), 400

    user_id, conflict = dhs_access.create_signup_request(name, email, password, institution, purpose, countries)
    if user_id is None:
        if conflict == 'pending':
            return jsonify({'ok': False, 'error': 'A request for this email is already pending review.'}), 409
        if conflict == 'approved':
            return jsonify({'ok': False, 'error': 'An account with this email already exists. Please sign in.'}), 409
        return jsonify({'ok': False, 'error': 'This email is already registered.'}), 409

    body = (
        f'A new DHS Explorer account request needs approval.\n\n'
        f'Name:        {name}\n'
        f'Email:       {email}\n'
        f'Institution: {institution}\n'
        f'Countries requested: {", ".join(countries) or "(not specified)"}\n\n'
        f'Purpose:\n{purpose or "(not provided)"}\n\n'
        f'Review at: {PORTAL_URL}/admin/\n'
        f'User ID: {user_id}'
    )
    for addr in ADMIN_EMAILS:
        _send_email(addr, f'[DHS Explorer] New account request from {name}', body)

    return jsonify({'ok': True, 'message': 'Your request has been submitted. You will receive an email once approved.'})


@app.route('/api/dhs/login', methods=['POST'])
def dhs_login():
    d        = request.json or {}
    email    = d.get('email', '').strip()
    password = d.get('password', '')

    if not email or not password:
        return jsonify({'ok': False, 'error': 'Email and password are required'}), 400

    existing = dhs_access.get_user_by_email(email)
    if existing and existing['status'] == 'pending':
        return jsonify({'ok': False, 'error': 'Your account is pending admin approval.'}), 403
    if existing and existing['status'] == 'rejected':
        return jsonify({'ok': False, 'error': 'Your access request was not approved. Contact the administrator.'}), 403
    if existing and existing['status'] == 'revoked':
        return jsonify({'ok': False, 'error': 'Your account has been revoked. Contact the administrator.'}), 403

    user = dhs_access.authenticate_user(email, password)
    if not user:
        return jsonify({'ok': False, 'error': 'Invalid email or password'}), 401

    token = dhs_access.issue_token(user['id'])
    return jsonify({'ok': True, 'token': token, 'name': user['name'], 'countries_allowed': user['countries_allowed']})


@app.route('/api/dhs/logout', methods=['POST'])
def dhs_logout():
    token = request.headers.get('X-Session-Token', '')
    if token:
        dhs_access.logout(token)
    return jsonify({'ok': True})


@app.route('/api/dhs/validate-session')
def dhs_validate_session():
    user, err = require_session()
    if err:
        return err
    return jsonify({'ok': True, 'name': user['name'], 'countries_allowed': user['countries_allowed']})


@app.route('/api/dhs/forgot-password', methods=['POST'])
def dhs_forgot_password():
    email = (request.json or {}).get('email', '').strip()
    if email:
        user = dhs_access.get_user_by_email(email)
        if user and user['status'] == 'approved':
            token = dhs_access.create_user_reset_token(user['id'])
            reset_url = f'{PORTAL_URL}/?reset_token={token}'
            _send_email(
                email, '[DHS Explorer] Password reset request',
                f'Hi {user["name"]},\n\nClick below to set a new password (valid for 1 hour):\n{reset_url}\n\n'
                f'If you did not request this, ignore this email.\n\nDHS Explorer'
            )
    return jsonify({'ok': True, 'message': 'If that email is registered and approved, a reset link has been sent.'})


@app.route('/api/dhs/reset-password', methods=['POST'])
def dhs_reset_password():
    d = request.json or {}
    token, password = d.get('token', '').strip(), d.get('password', '')
    if not token or not password:
        return jsonify({'ok': False, 'error': 'Token and password are required'}), 400
    if len(password) < 8:
        return jsonify({'ok': False, 'error': 'Password must be at least 8 characters'}), 400
    user_id = dhs_access.consume_user_reset_token(token, password)
    if not user_id:
        return jsonify({'ok': False, 'error': 'Reset link is invalid or has expired'}), 400
    return jsonify({'ok': True, 'message': 'Password updated. You can now sign in.'})


# ── Admin-issued invites (claim flow) ──────────────────────────────────────────

@app.route('/api/dhs/invite/<token>')
def dhs_get_invite(token):
    invite = dhs_access.get_invite(token)
    if not dhs_access.invite_is_valid(invite):
        return jsonify({'ok': False, 'error': 'This invite link is invalid, expired, or already used.'}), 404
    return jsonify({
        'ok': True, 'email': invite['email'], 'name': invite['name'],
        'countries_allowed': invite['countries_allowed'],
    })


@app.route('/api/dhs/invite/<token>/claim', methods=['POST'])
def dhs_claim_invite(token):
    password = (request.json or {}).get('password', '')
    if len(password) < 8:
        return jsonify({'ok': False, 'error': 'Password must be at least 8 characters'}), 400
    user = dhs_access.claim_invite(token, password)
    if not user:
        return jsonify({'ok': False, 'error': 'This invite link is invalid, expired, or already used.'}), 404
    session_token = dhs_access.issue_token(user['id'])
    return jsonify({'ok': True, 'token': session_token, 'name': user['name'],
                     'countries_allowed': user['countries_allowed']})


# ══════════════════════════════════════════════════════════════════════════════
# Admin console
# ══════════════════════════════════════════════════════════════════════════════

def _public_user(u):
    """Strip the bcrypt password hash before a user dict goes out over the API —
    the admin UI never needs it, so don't expose it even hashed."""
    if not u:
        return u
    return {k: v for k, v in u.items() if k != 'password'}


@app.route('/api/dhs/admin/login', methods=['POST'])
def admin_login():
    d = request.json or {}
    username, password = d.get('username', '').strip(), d.get('password', '')
    ok, must_change = dhs_access.check_admin(username, password)
    if not ok:
        return jsonify({'ok': False, 'error': 'Invalid credentials'}), 401
    token = dhs_access.issue_admin_token(username)
    return jsonify({'ok': True, 'token': token, 'must_change_pw': must_change})


@app.route('/api/dhs/admin/logout', methods=['POST'])
def admin_logout_route():
    token = request.headers.get('X-Admin-Token', '')
    if token:
        dhs_access.admin_logout(token)
    return jsonify({'ok': True})


@app.route('/api/dhs/admin/change-password', methods=['POST'])
def admin_change_password():
    username, err = require_admin()
    if err:
        return err
    new_password = (request.json or {}).get('new_password', '')
    if len(new_password) < 8:
        return jsonify({'ok': False, 'error': 'Password must be at least 8 characters'}), 400
    dhs_access.set_admin_password(username, new_password)
    return jsonify({'ok': True})


@app.route('/api/dhs/admin/users')
def admin_list_users():
    _, err = require_admin()
    if err:
        return err
    status = request.args.get('status')
    return jsonify({'users': [_public_user(u) for u in dhs_access.get_users(status)]})


@app.route('/api/dhs/admin/users/<user_id>/approve', methods=['POST'])
def admin_approve_user(user_id):
    _, err = require_admin()
    if err:
        return err
    countries = (request.json or {}).get('countries', [])
    user = dhs_access.approve_user(user_id, countries)
    if not user:
        return jsonify({'ok': False, 'error': 'User not found'}), 404
    _send_email(
        user['email'], '[DHS Explorer] Your access request has been approved',
        f'Hi {user["name"]},\n\nYour DHS Explorer account has been approved for: '
        f'{", ".join(countries) or "(no countries set)"}.\n\nSign in at {PORTAL_URL}\n\nDHS Explorer'
    )
    return jsonify({'ok': True, 'user': _public_user(user)})


@app.route('/api/dhs/admin/users/<user_id>/reject', methods=['POST'])
def admin_reject_user(user_id):
    _, err = require_admin()
    if err:
        return err
    notes = (request.json or {}).get('notes', '')
    user = dhs_access.reject_user(user_id, notes)
    if not user:
        return jsonify({'ok': False, 'error': 'User not found'}), 404
    return jsonify({'ok': True, 'user': _public_user(user)})


@app.route('/api/dhs/admin/users/<user_id>/revoke', methods=['POST'])
def admin_revoke_user(user_id):
    _, err = require_admin()
    if err:
        return err
    user = dhs_access.revoke_user(user_id)
    if not user:
        return jsonify({'ok': False, 'error': 'User not found'}), 404
    return jsonify({'ok': True, 'user': _public_user(user)})


@app.route('/api/dhs/admin/users/<user_id>/reinstate', methods=['POST'])
def admin_reinstate_user(user_id):
    _, err = require_admin()
    if err:
        return err
    user = dhs_access.reinstate_user(user_id)
    if not user:
        return jsonify({'ok': False, 'error': 'User not found'}), 404
    return jsonify({'ok': True, 'user': _public_user(user)})


@app.route('/api/dhs/admin/users/<user_id>/countries', methods=['POST'])
def admin_update_user_countries(user_id):
    _, err = require_admin()
    if err:
        return err
    countries = (request.json or {}).get('countries', [])
    user = dhs_access.update_user_countries(user_id, countries)
    if not user:
        return jsonify({'ok': False, 'error': 'User not found'}), 404
    return jsonify({'ok': True, 'user': _public_user(user)})


@app.route('/api/dhs/admin/invites', methods=['GET'])
def admin_list_invites():
    _, err = require_admin()
    if err:
        return err
    return jsonify({'invites': dhs_access.list_invites()})


@app.route('/api/dhs/admin/invites', methods=['POST'])
def admin_create_invite():
    username, err = require_admin()
    if err:
        return err
    d = request.json or {}
    email     = d.get('email', '').strip()
    name      = d.get('name', '').strip()
    countries = d.get('countries', [])
    if not email:
        return jsonify({'ok': False, 'error': 'Email is required'}), 400

    token = dhs_access.create_invite(email, name, countries, username)
    claim_url = f'{PORTAL_URL}/claim.html?token={token}'
    _send_email(
        email, "[DHS Explorer] You've been invited",
        f'Hi{" " + name if name else ""},\n\n'
        f'You have been invited to the DHS Explorer research portal, approved for: '
        f'{", ".join(countries) or "(no countries set)"}.\n\n'
        f'Set your password and get started here (link valid for {dhs_access._INVITE_TTL_DAYS} days):\n'
        f'{claim_url}\n\nDHS Explorer'
    )
    return jsonify({'ok': True, 'token': token, 'claim_url': claim_url})


@app.route('/api/dhs/admin/invites/<token>/revoke', methods=['POST'])
def admin_revoke_invite(token):
    _, err = require_admin()
    if err:
        return err
    dhs_access.revoke_invite(token)
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════════════
# Data endpoints (session required)
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/api/dhs/schema')
def dhs_schema():
    user, err = require_session()
    if err:
        return err
    countries = user['countries_allowed']

    conn = _open_conn()
    try:
        tables = []
        for code, (_dbfile, table, title, desc) in dhs_codebook.RECODES.items():
            try:
                filtered = apply_dhs_country_filter(f'SELECT COUNT(*) FROM "{table}"', countries)
                n = conn.execute(filtered).fetchone()[0]
            except Exception:
                n = None
            tables.append({'recode': code, 'table': table, 'title': title, 'description': desc, 'row_count': n})
        return jsonify({'tables': tables, 'authorised_countries': countries})
    finally:
        conn.close()


@app.route('/api/dhs/query', methods=['POST'])
def dhs_query():
    user, err = require_session()
    if err:
        return err

    data = request.json or {}
    sql  = data.get('sql', '').strip()
    if not sql:
        return jsonify({'error': 'No SQL provided'}), 400

    safe, reason = is_safe_query(sql)
    if not safe:
        return jsonify({'error': reason}), 400

    max_rows = data.get('max_rows')
    filtered_sql = apply_dhs_country_filter(sql, user['countries_allowed'])

    conn = _open_conn()
    try:
        cur = conn.execute(filtered_sql)
        columns = [d[0] for d in cur.description]
        raw = cur.fetchmany(int(max_rows)) if max_rows else cur.fetchall()
        rows = [[_coerce(v) for v in row] for row in raw]
        _log_query(request.headers.get('X-Session-Token', ''), user['id'], 'query', sql=filtered_sql, rows=len(rows))
        return jsonify({
            'columns': columns, 'rows': rows, 'row_count': len(rows),
            'truncated': bool(max_rows and len(rows) == int(max_rows)),
            'authorised_countries': user['countries_allowed'],
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    finally:
        conn.close()


@app.route('/api/dhs/query/csv', methods=['POST'])
def dhs_query_csv():
    user, err = require_session()
    if err:
        return err

    data = request.json or {}
    sql  = data.get('sql', '').strip()
    if not sql:
        return jsonify({'error': 'No SQL provided'}), 400

    safe, reason = is_safe_query(sql)
    if not safe:
        return jsonify({'error': reason}), 400

    max_rows = data.get('max_rows')
    if max_rows:
        sql = f"SELECT * FROM ({sql}) __q LIMIT {int(max_rows)}"
    filtered_sql = apply_dhs_country_filter(sql, user['countries_allowed'])

    conn = _open_conn()
    try:
        cur = conn.execute(filtered_sql)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()

        buf = io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow(columns)
        writer.writerows(rows)
        _log_query(request.headers.get('X-Session-Token', ''), user['id'], 'query_csv', sql=filtered_sql, rows=len(rows))

        return Response(
            buf.getvalue(), mimetype='text/csv',
            headers={
                'X-Row-Count': str(len(rows)),
                'X-Authorised-Countries': ','.join(user['countries_allowed']),
                'Content-Disposition': 'attachment; filename="dhs_extract.csv"',
            }
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 400
    finally:
        conn.close()


@app.route('/api/dhs/coverage')
def dhs_coverage():
    """Aggregate coordinate coverage — country/survey/year counts, powers the
    map's summary view. Use /api/dhs/coverage/points for actual point data."""
    user, err = require_session()
    if err:
        return err

    conn = _open_conn()
    try:
        filtered = apply_dhs_country_filter('''
            SELECT country_code, country_name, survey_code, dhs_phase, dhsyear,
                   COUNT(*) AS n_clusters,
                   AVG(latnum) AS centroid_lat, AVG(longnum) AS centroid_lon
            FROM clusters
            GROUP BY 1, 2, 3, 4, 5
            ORDER BY country_name, dhsyear
        ''', user['countries_allowed'])
        rows = conn.execute(filtered).fetchall()
        cols = ['country_code', 'country_name', 'survey_code', 'dhs_phase', 'dhsyear',
                'n_clusters', 'centroid_lat', 'centroid_lon']
        return jsonify({'coverage': [dict(zip(cols, [_coerce(v) for v in r])) for r in rows]})
    finally:
        conn.close()


@app.route('/api/dhs/coverage/points')
def dhs_coverage_points():
    """Raw cluster points for one survey — bounded by construction (a single
    DHS survey is at most a few thousand clusters), never the full 78k table."""
    user, err = require_session()
    if err:
        return err
    survey_code = request.args.get('survey_code', '').strip()
    if not survey_code:
        return jsonify({'error': 'survey_code is required'}), 400

    conn = _open_conn()
    try:
        filtered = apply_dhs_country_filter(
            'SELECT dhsclust, latnum, longnum, urban_rura, adm1name '
            'FROM clusters WHERE survey_code = ? AND latnum IS NOT NULL AND latnum != 0',
            user['countries_allowed']
        )
        rows = conn.execute(filtered, [survey_code]).fetchall()
        points = [
            {'cluster': r[0], 'lat': r[1], 'lon': r[2], 'urban_rural': r[3], 'region': r[4]}
            for r in rows
        ]
        return jsonify({'survey_code': survey_code, 'points': points})
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# DHS Explorer — chat agent
#
# Unlike Shmron (~/PRECISE/api.py), the schema does NOT fit in the system
# prompt: dhs_ir.duckdb alone has 60,609 columns across 13 tables. Instead the
# agent gets schema-lookup tools it calls on demand (search_variables,
# describe_table) plus a dedicated get_coordinate_coverage tool so it can
# proactively answer "what has coordinates" rather than waiting to be asked.
# ══════════════════════════════════════════════════════════════════════════════

def _dhs_chat_system_prompt(countries_allowed):
    return f"""You are DHS Explorer — a friendly, patient guide to a pooled Demographic and Health Surveys \
(DHS) dataset spanning many countries and survey years. Assume the person asking has ZERO background in DHS \
data: they may be a public-health researcher, student, or clinician who has never seen a DHS recode file in \
their life. Never assume they know what "IR", "v001", or "recode" mean -- translate everything into plain \
language first, and only mention the underlying DHS variable code afterward, in parentheses, as a reference \
for anyone who does know DHS conventions. Think of yourself as walking someone through unfamiliar terrain \
they've never explored before: narrate what you're about to check and why, in one short friendly sentence, \
before diving into results.

**FORMATTING -- your output is rendered as markdown, use it properly:**
- Lead with a one-line plain-English answer before any detail.
- Use real markdown tables (pipe-delimited) for anything comparative or tabular -- never describe a table in prose.
- Keep tables short and scannable: if there would be more than ~12 rows, show the most relevant ones and say \
  how many more there are ("...and 20 more countries -- ask if you want the full list").
- Use bold for key numbers and short bullet lists for next-step suggestions (2-4 bullets, not a wall of options).
- Avoid dumping raw column names or SQL in your prose response -- that's for the tool calls, not the answer.

**HANDLING COMPOUND / EPIDEMIOLOGY-STYLE QUESTIONS** -- e.g. "how many records have low birthweight, with \
location and maternal health outcome": these are exactly what this portal is for, and the person asking may \
not know which module holds what. Don't wait to be given exact variable names.
- Silently break the question into its parts first (an outcome, a geography, and possibly a covariate/second \
  outcome), then say what you're doing in ONE short sentence -- e.g. "Let's find the birthweight records, add \
  their village location, and see what maternal outcomes are recorded alongside them."
- Use search_variables for each part you're unsure of rather than guessing a column name -- try more than one \
  keyword if the first search comes up empty (e.g. for birthweight also try "m19" or "weight").
- Join across recodes using the shared identification scheme below (cluster number at minimum; case/household \
  linkage where the modules share it, e.g. births to children's recode via the birth index).
- After the query, translate the result back into plain language, not just a number -- e.g. "412 of 3,108 \
  babies with a recorded weight (13%) were low birthweight (under 2.5kg)."

**COMMON MATERNAL & CHILD HEALTH OUTCOMES -- a starting cheat-sheet (verify with search_variables/describe_table \
before relying on these, DHS variable names shift slightly by survey phase):**
- Low birthweight: `births` or `children` table, `m19` (birthweight in kg) + `m19a` (unit code); DHS/WHO \
  standard threshold is under 2.5 kg. Many births have no card-recorded weight -- always report the denominator \
  (how many had a valid weight recorded), not just the low-birthweight count.
- Child growth/malnutrition: `anthropometry` (HW recode) -- `haz`/`whz`/`waz` (height/weight/weight-height \
  z-scores, WHO standards); a child is stunted if HAZ < -2, wasted if WHZ < -2, underweight if WAZ < -2.
- Antenatal care & delivery: `women` (IR) `m14` (number of ANC visits), `m15` (place of delivery), `m17` \
  (delivery by caesarean).
- Anaemia: `women` (IR) `v456`/`v457` (haemoglobin level / anaemia level).
- HIV: `hiv_results` (AR) `hiv03`/`hiv05` (HIV test result).
- Household/geographic context: `v025`/`hv025` (urban/rural) is on most recodes directly -- you don't need to \
  join to `clusters` just for urban/rural, only for actual coordinates.

**CRITICAL RULES:**
1. You MUST call execute_query for any question about counts, distributions, or statistics — never guess \
   or estimate a number yourself.
2. Tables are WIDE and use terse DHS variable codes (v001, v106, hv270, etc). Before writing SQL against a \
   column you are not certain exists, call search_variables to find the right column name and its meaning. \
   For the very wide tables (women, persons, households — tens of thousands of columns), always search first.
3. Queries are automatically restricted server-side to this session's approved countries \
   ({', '.join(countries_allowed) or '(none)'}) — you do not need to add your own country filter, though it's \
   harmless if you do. If asked about a country outside this list, say so plainly rather than trying to query it.
4. **PRIVACY — never quote a respondent's caseid, hhid, or household/line number (v002/v003/hv002/hv003).** \
   These are stripped from tool results before you see them. The DHS cluster number (v001/hv001/dhsclust) is \
   NOT stripped — it identifies a whole community (a village or urban block, never one household) and is the \
   standard key for joining to GPS coordinates in the `clusters` table (dhs_ge), which is central to what this \
   portal is for.
5. **COORDINATES:** `clusters` (GE recode) is the primary source of usable GPS points (latnum/longnum), keyed \
   by dhsclust = v001/hv001 in the other recodes. Mention when relevant that DHS deliberately displaces these \
   coordinates for respondent anonymity (~2km urban / 5km rural, 10km for 1% of rural clusters) — they are \
   approximate, not exact locations. Use get_coordinate_coverage to answer "what has coordinates" questions \
   without writing raw SQL.
6. When a question is broad or exploratory ("what's here", "what can I do"), proactively mention that you can \
   run live queries, search variable definitions, show coordinate coverage, render a map or chart, and export \
   filtered extracts as CSV — don't just wait to be asked one at a time.
7. Use render_chart (bar/line/pie/doughnut/map) alongside execute_query when a visual would help — especially \
   `map` for anything geographic (pass points from a coordinate-aware query or from get_coordinate_coverage).

=== DHS MODULE MAP (13 tables — call describe_table or search_variables before querying an unfamiliar one) ===

{dhs_codebook.module_map_text()}

=== SHARED IDENTIFICATION SCHEME (present on every table) ===
- country_code (ISO2), country_name, survey_code (country+recode+phase, e.g. 'KEBR8C'), dhs_phase (1-8)
- v001 / hv001 / dhsclust — cluster number, the join key to `clusters` for coordinates
- v007 / hv007 — year of interview; dhsyear on `clusters`

Currently approved countries for this session: {', '.join(countries_allowed) or '(none — contact the admin)'}"""


_DHS_CHAT_TOOLS = [
    {
        "name": "execute_query",
        "description": "Run a read-only SQL SELECT against the DHS tables. Table names: " +
                        ', '.join(_DHS_TABLES) + ". You can JOIN across tables using the cluster number "
                        "(v001/hv001/dhsclust) or country_code+survey_code. Country scoping is applied "
                        "automatically server-side. Results are capped at 50 rows for you to read — use "
                        "aggregation (COUNT/AVG/GROUP BY) rather than dumping raw rows.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "A DuckDB-compatible SELECT query."}
            },
            "required": ["sql"]
        }
    },
    {
        "name": "search_variables",
        "description": "Look up DHS variable names and their meaning from the codebook by keyword — use this "
                        "before querying a column you're not 100% sure exists, especially on the wide tables "
                        "(women, persons, households). Searches both variable codes (e.g. 'v106') and plain-"
                        "language labels (e.g. 'education').",
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "A variable code or plain-language term to search for."}
            },
            "required": ["keyword"]
        }
    },
    {
        "name": "describe_table",
        "description": "List the columns and types of one DHS table. Use for the narrower tables, or after "
                        "search_variables has narrowed down which table you need. On the very wide tables this "
                        "returns a lot of columns — prefer search_variables first for those.",
        "input_schema": {
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "One of: " + ', '.join(_DHS_TABLES)}
            },
            "required": ["table"]
        }
    },
    {
        "name": "get_coordinate_coverage",
        "description": "Return cluster counts (and centroid) by country/survey/year from the coordinates table "
                        "— the fast way to answer 'what has coordinates' without writing SQL. Optionally filter "
                        "to one country.",
        "input_schema": {
            "type": "object",
            "properties": {
                "country_code": {"type": "string", "description": "Optional ISO2 country code to filter to."}
            },
            "required": []
        }
    },
    {
        "name": "render_chart",
        "description": "Render an interactive chart inline in the conversation. Call in the SAME response as "
                        "execute_query or get_coordinate_coverage when a visual would help. Use chart_type='map' "
                        "for anything geographic.",
        "input_schema": {
            "type": "object",
            "properties": {
                "chart_type": {"type": "string", "enum": ["bar", "line", "pie", "doughnut", "map"]},
                "title": {"type": "string"},
                "labels": {"type": "array", "items": {"type": "string"},
                           "description": "Category labels for bar/line/pie/doughnut"},
                "datasets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "data": {"type": "array", "items": {"type": "number"}}
                        },
                        "required": ["label", "data"]
                    }
                },
                "map_data": {
                    "type": "array",
                    "description": "Points for a geographic map — lat/lon plus a label and optional numeric "
                                    "value for colour (e.g. n_clusters, a rate, a mean exposure).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "lat": {"type": "number"}, "lon": {"type": "number"},
                            "label": {"type": "string"}, "value": {"type": "number"},
                            "group": {"type": "string"}
                        },
                        "required": ["lat", "lon", "label"]
                    }
                },
                "color_label": {"type": "string"}, "x_label": {"type": "string"}, "y_label": {"type": "string"}
            },
            "required": ["chart_type", "title"]
        }
    },
]


@app.route('/api/dhs/chat', methods=['POST'])
def dhs_chat():
    user, err = require_session()
    if err:
        return err

    ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown').split(',')[0].strip()
    if not _allow_ip(ip):
        return jsonify({'error': 'Rate limit exceeded — please slow down.'}), 429

    data     = request.json or {}
    messages = data.get('messages', [])
    if not messages or messages[-1].get('role') != 'user':
        return jsonify({'error': 'messages must end with a user turn'}), 400

    countries  = user['countries_allowed']
    sess_token = request.headers.get('X-Session-Token', '')

    def generate():
        try:
            import anthropic as _anthropic
            client = _anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

            history = list(messages)
            # Compound questions (e.g. "low birthweight by location and maternal
            # outcome") routinely take 8-12+ tool calls, including a couple of
            # SQL retries after a syntax/column-name miss — 8 was cutting real
            # answers off mid-investigation with no explanation to the user.
            max_iterations = 16
            answered = False

            for i in range(max_iterations):
                tool_choice = {'type': 'any'} if i == 0 else {'type': 'auto'}
                status = 'Thinking…' if i == 0 else 'Analysing…'
                yield f"data: {json.dumps({'type': 'status', 'text': status})}\n\n"

                response = client.messages.create(
                    model='claude-sonnet-5',
                    max_tokens=4096,
                    system=_dhs_chat_system_prompt(countries),
                    messages=history,
                    tools=_DHS_CHAT_TOOLS,
                    tool_choice=tool_choice,
                )

                tool_blocks = [b for b in response.content if b.type == 'tool_use']
                text_blocks = [b for b in response.content if b.type == 'text']
                final_text  = text_blocks[0].text if text_blocks else ''

                assistant_content = []
                for b in response.content:
                    if b.type == 'text':
                        assistant_content.append({'type': 'text', 'text': b.text})
                    elif b.type == 'tool_use':
                        assistant_content.append({'type': 'tool_use', 'id': b.id, 'name': b.name, 'input': b.input})
                history.append({'role': 'assistant', 'content': assistant_content})

                if not tool_blocks:
                    yield f"data: {json.dumps({'type': 'text', 'text': final_text})}\n\n"
                    answered = True
                    break

                tool_results = []
                conn = _open_conn()
                try:
                    for block in tool_blocks:
                        if block.name == 'render_chart':
                            yield f"data: {json.dumps({'type': 'chart', 'spec': block.input})}\n\n"
                            result = {'rendered': True}

                        elif block.name == 'search_variables':
                            keyword = block.input.get('keyword', '')
                            matches = dhs_codebook.search_variables(keyword, limit=20)
                            result = {'matches': matches, 'count': len(matches)}
                            _log_query(sess_token, user['id'], 'search_variables', keyword=keyword, n=len(matches))

                        elif block.name == 'describe_table':
                            table = block.input.get('table', '')
                            if table not in _DHS_TABLES:
                                result = {'error': f"Unknown table '{table}'. Valid tables: {', '.join(_DHS_TABLES)}"}
                            else:
                                cols = conn.execute(f'DESCRIBE "{table}"').fetchall()
                                result = {'table': table, 'columns': [{'name': c[0], 'type': c[1]} for c in cols],
                                          'n_columns': len(cols)}
                            _log_query(sess_token, user['id'], 'describe_table', table=table)

                        elif block.name == 'get_coordinate_coverage':
                            filter_country = block.input.get('country_code')
                            sql = ('SELECT country_code, country_name, survey_code, dhs_phase, dhsyear, '
                                   'COUNT(*) n_clusters, AVG(latnum) centroid_lat, AVG(longnum) centroid_lon '
                                   'FROM clusters ')
                            params = []
                            if filter_country:
                                sql += 'WHERE country_code = ? '
                                params.append(filter_country)
                            sql += 'GROUP BY 1,2,3,4,5 ORDER BY country_name, dhsyear'
                            filtered = apply_dhs_country_filter(sql, countries)
                            rows = conn.execute(filtered, params).fetchall()
                            cols = ['country_code', 'country_name', 'survey_code', 'dhs_phase', 'dhsyear',
                                    'n_clusters', 'centroid_lat', 'centroid_lon']
                            result = {'coverage': [dict(zip(cols, [_coerce(v) for v in r])) for r in rows]}
                            _log_query(sess_token, user['id'], 'get_coordinate_coverage',
                                       country=filter_country, n=len(rows))

                        else:  # execute_query
                            sql = block.input.get('sql', '')
                            safe, reason = is_safe_query(sql)
                            if not safe:
                                result = {'error': reason}
                                _log_query(sess_token, user['id'], 'execute_query', sql=sql, error=f'blocked: {reason}')
                            else:
                                filtered = apply_dhs_country_filter(sql, countries)
                                try:
                                    cur = conn.execute(filtered)
                                    cols = [d[0] for d in cur.description]
                                    rows = cur.fetchmany(50)
                                    result = _strip_id_cols({
                                        'columns': cols,
                                        'rows': [{cols[j]: _coerce(v) for j, v in enumerate(r)} for r in rows],
                                        'row_count': len(rows),
                                    })
                                    _log_query(sess_token, user['id'], 'execute_query', sql=filtered, rows=len(rows))
                                except Exception as e:
                                    result = {'error': str(e)}
                                    _log_query(sess_token, user['id'], 'execute_query', sql=filtered, error=str(e))

                        tool_results.append({
                            'type': 'tool_result', 'tool_use_id': block.id, 'content': json.dumps(result),
                        })
                finally:
                    conn.close()

                history.append({'role': 'user', 'content': tool_results})

            if not answered:
                # Ran out of tool-call rounds mid-investigation (compound questions
                # can legitimately need many). Force one final call with no tools so
                # the user gets a real answer from whatever was found so far,
                # instead of the request silently going nowhere.
                yield f"data: {json.dumps({'type': 'status', 'text': 'Wrapping up…'})}\n\n"
                history.append({'role': 'user', 'content':
                    "You're out of further tool calls for this turn. Summarise what you've found so far "
                    "in plain language, using whatever partial results you already have. If you didn't "
                    "get a complete answer, say clearly what's still missing and suggest a narrower "
                    "follow-up question."})
                fallback_text = "I wasn't able to finish this one — try breaking it into a narrower question."
                try:
                    response = client.messages.create(
                        model='claude-sonnet-5',
                        max_tokens=1024,
                        system=_dhs_chat_system_prompt(countries),
                        messages=history,
                    )
                    text_blocks = [b for b in response.content if b.type == 'text']
                    final_text = text_blocks[0].text if text_blocks else fallback_text
                    yield f"data: {json.dumps({'type': 'text', 'text': final_text})}\n\n"
                except Exception:
                    yield f"data: {json.dumps({'type': 'text', 'text': fallback_text})}\n\n"

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'text': str(e)})}\n\n"

    return Response(
        generate(), mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


if __name__ == '__main__':
    # threaded=True: the chat endpoint streams SSE responses that can run for many
    # seconds — without this, Flask's dev server is single-threaded and one user's
    # in-flight chat would block every other request on the box.
    app.run(host='0.0.0.0', port=5100, debug=False, threaded=True)
