"""
Access control database for the DHS Explorer portal.
SQLite-backed store for researcher accounts, admin-issued invites, and sessions.

Modeled on ~/PRECISE/access_db.py's catalogue_users / admins pattern, trimmed to
just what this standalone portal needs, plus an admin-invite flow (create an
account directly rather than only accepting self-signup + approval).
"""
import sqlite3, secrets, json, datetime, os
import bcrypt as _bcrypt

DB_PATH    = '/home/USER/dhs_portal/dhs_access.db'
_PASS_FILE = '/home/USER/dhs_portal/.admin_pass'

_TOKEN_TTL          = 8 * 3600      # session token lifetime, seconds
_RESET_TOKEN_TTL     = 3600          # password reset link lifetime, seconds
_INVITE_TTL_DAYS     = 14            # admin invite link lifetime, days


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _hash_pw(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()


def _verify_pw(password: str, stored: str) -> bool:
    b = stored.encode() if isinstance(stored, str) else stored
    if not b.startswith(b'$2'):
        return password == stored
    return _bcrypt.checkpw(password.encode(), b)


def _now():
    return datetime.datetime.utcnow().isoformat()


def init_db():
    with _conn() as c:
        c.executescript('''
            CREATE TABLE IF NOT EXISTS admins (
                username       TEXT PRIMARY KEY,
                password       TEXT NOT NULL,
                email          TEXT,
                must_change_pw INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS admin_reset_tokens (
                token      TEXT PRIMARY KEY,
                username   TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_tokens (
                token      TEXT PRIMARY KEY,
                username   TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dhs_users (
                id                TEXT PRIMARY KEY,
                name              TEXT NOT NULL,
                email             TEXT NOT NULL UNIQUE,
                password          TEXT NOT NULL,
                institution       TEXT,
                purpose           TEXT,
                countries_allowed TEXT DEFAULT '[]',
                status            TEXT DEFAULT 'pending',   -- pending/approved/rejected/revoked
                is_active         INTEGER DEFAULT 0,
                invited_by        TEXT,                     -- admin username, if created via invite
                created_at        TEXT NOT NULL,
                reviewed_at       TEXT,
                last_used         TEXT,
                notes             TEXT
            );
            CREATE TABLE IF NOT EXISTS dhs_invites (
                token             TEXT PRIMARY KEY,
                email             TEXT NOT NULL,
                name              TEXT,
                countries_allowed TEXT DEFAULT '[]',
                created_by        TEXT NOT NULL,
                created_at        TEXT NOT NULL,
                expires_at        TEXT NOT NULL,
                claimed_at        TEXT,
                claimed_user_id   TEXT
            );
            CREATE TABLE IF NOT EXISTS dhs_tokens (
                token      TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dhs_reset_tokens (
                token      TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
        ''')
    _seed_admin()


# ── Admin accounts ────────────────────────────────────────────────────────────

def _seed_admin():
    """Create the default 'rutendo' admin on first run with a fresh random
    password written to _PASS_FILE (chmod 600) — never hardcoded, never guessed."""
    with _conn() as c:
        existing = c.execute("SELECT 1 FROM admins WHERE username='rutendo'").fetchone()
        if existing:
            return
        raw_pw = secrets.token_urlsafe(18)
        c.execute(
            "INSERT INTO admins (username, password, email, must_change_pw) VALUES (?,?,?,0)",
            ('rutendo', _hash_pw(raw_pw), 'admin@example.org')
        )
    with open(_PASS_FILE, 'w') as f:
        f.write(raw_pw + '\n')
    os.chmod(_PASS_FILE, 0o600)


def check_admin(username, password):
    """Return (True, must_change_pw) if credentials match, else (False, False)."""
    with _conn() as c:
        row = c.execute(
            "SELECT password, must_change_pw FROM admins WHERE username=?", (username,)
        ).fetchone()
    if not row or not _verify_pw(password, row['password']):
        return False, False
    return True, bool(row['must_change_pw'])


def set_admin_password(username, new_pass):
    with _conn() as c:
        c.execute(
            "UPDATE admins SET password=?, must_change_pw=0 WHERE username=?",
            (_hash_pw(new_pass), username)
        )


def add_admin(username, password, email=None):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO admins (username, password, email, must_change_pw) VALUES (?,?,?,1)",
            (username, _hash_pw(password), email)
        )


def get_admin_email(username):
    with _conn() as c:
        row = c.execute("SELECT email FROM admins WHERE username=?", (username,)).fetchone()
    return row['email'] if row else None


def issue_admin_token(username):
    token   = 'adm_' + secrets.token_urlsafe(32)
    now     = _now()
    expires = (datetime.datetime.utcnow() +
               datetime.timedelta(seconds=_TOKEN_TTL)).isoformat()
    with _conn() as c:
        c.execute("DELETE FROM admin_tokens WHERE expires_at < ?", (now,))
        c.execute(
            "INSERT INTO admin_tokens (token, username, expires_at) VALUES (?,?,?)",
            (token, username, expires)
        )
    return token


def get_admin_from_token(token):
    if not token:
        return None
    now = _now()
    with _conn() as c:
        row = c.execute(
            "SELECT username FROM admin_tokens WHERE token=? AND expires_at > ?",
            (token, now)
        ).fetchone()
    return row['username'] if row else None


def admin_logout(token):
    with _conn() as c:
        c.execute("DELETE FROM admin_tokens WHERE token=?", (token,))


def list_admins():
    with _conn() as c:
        rows = c.execute("SELECT username, email FROM admins ORDER BY username").fetchall()
    return [dict(r) for r in rows]


def create_admin_reset_token(username):
    token   = secrets.token_urlsafe(32)
    expires = (datetime.datetime.utcnow() +
               datetime.timedelta(seconds=_RESET_TOKEN_TTL)).isoformat()
    with _conn() as c:
        c.execute("DELETE FROM admin_reset_tokens WHERE username=?", (username,))
        c.execute(
            "INSERT INTO admin_reset_tokens (token, username, expires_at) VALUES (?,?,?)",
            (token, username, expires)
        )
    return token


def consume_admin_reset_token(token, new_password):
    now = _now()
    with _conn() as c:
        row = c.execute(
            "SELECT username, expires_at FROM admin_reset_tokens WHERE token=?", (token,)
        ).fetchone()
    if not row or now > row['expires_at']:
        return None
    set_admin_password(row['username'], new_password)
    with _conn() as c:
        c.execute("DELETE FROM admin_reset_tokens WHERE token=?", (token,))
    return row['username']


# ── Researcher self-signup ─────────────────────────────────────────────────────

def create_signup_request(name, email, password, institution='', purpose='', countries_requested=None):
    """Returns (user_id, None) on success, or (None, existing_status) if the
    email is already registered (pending/approved/rejected/revoked)."""
    email = email.lower().strip()
    with _conn() as c:
        existing = c.execute("SELECT status FROM dhs_users WHERE email=?", (email,)).fetchone()
        if existing:
            return None, existing['status']
    user_id = secrets.token_hex(8)
    now = _now()
    with _conn() as c:
        c.execute(
            '''INSERT INTO dhs_users
               (id, name, email, password, institution, purpose, countries_allowed,
                status, is_active, created_at, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (user_id, name, email, _hash_pw(password), institution, purpose,
             json.dumps(countries_requested or []), 'pending', 0, now,
             'requested_countries=' + json.dumps(countries_requested or []))
        )
    return user_id, None


def get_user_by_email(email):
    with _conn() as c:
        row = c.execute("SELECT * FROM dhs_users WHERE email=?", (email.lower().strip(),)).fetchone()
    return _user_dict(row)


def get_user(user_id):
    with _conn() as c:
        row = c.execute("SELECT * FROM dhs_users WHERE id=?", (user_id,)).fetchone()
    return _user_dict(row)


def _user_dict(row):
    if not row:
        return None
    d = dict(row)
    try:
        d['countries_allowed'] = json.loads(d['countries_allowed'] or '[]')
    except Exception:
        d['countries_allowed'] = []
    return d


def get_users(status=None):
    with _conn() as c:
        if status:
            rows = c.execute(
                "SELECT * FROM dhs_users WHERE status=? ORDER BY created_at DESC", (status,)
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM dhs_users ORDER BY created_at DESC").fetchall()
    return [_user_dict(r) for r in rows]


def authenticate_user(email, password):
    """Return user dict if credentials match and account is approved+active, else None."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM dhs_users WHERE email=? AND status='approved' AND is_active=1",
            (email.lower().strip(),)
        ).fetchone()
    if not row:
        return None
    user = _user_dict(row)
    if not _verify_pw(password, user['password']):
        return None
    return user


def approve_user(user_id, countries_allowed):
    now = _now()
    with _conn() as c:
        c.execute(
            "UPDATE dhs_users SET status='approved', is_active=1, countries_allowed=?, reviewed_at=? WHERE id=?",
            (json.dumps(countries_allowed), now, user_id)
        )
    return get_user(user_id)


def reject_user(user_id, notes=''):
    now = _now()
    with _conn() as c:
        c.execute(
            "UPDATE dhs_users SET status='rejected', is_active=0, reviewed_at=?, notes=? WHERE id=?",
            (now, notes, user_id)
        )
    return get_user(user_id)


def revoke_user(user_id):
    now = _now()
    with _conn() as c:
        c.execute(
            "UPDATE dhs_users SET status='revoked', is_active=0, reviewed_at=? WHERE id=?",
            (now, user_id)
        )
        c.execute("DELETE FROM dhs_tokens WHERE user_id=?", (user_id,))
    return get_user(user_id)


def reinstate_user(user_id):
    now = _now()
    with _conn() as c:
        c.execute(
            "UPDATE dhs_users SET status='approved', is_active=1, reviewed_at=? WHERE id=?",
            (now, user_id)
        )
    return get_user(user_id)


def update_user_countries(user_id, countries_allowed):
    with _conn() as c:
        c.execute(
            "UPDATE dhs_users SET countries_allowed=? WHERE id=?",
            (json.dumps(countries_allowed), user_id)
        )
    return get_user(user_id)


# ── Sessions ────────────────────────────────────────────────────────────────

def issue_token(user_id):
    token   = 'dhs_' + secrets.token_urlsafe(32)
    now     = _now()
    expires = (datetime.datetime.utcnow() +
               datetime.timedelta(seconds=_TOKEN_TTL)).isoformat()
    with _conn() as c:
        c.execute("DELETE FROM dhs_tokens WHERE expires_at < ?", (now,))
        c.execute(
            "INSERT INTO dhs_tokens (token, user_id, expires_at) VALUES (?,?,?)",
            (token, user_id, expires)
        )
        c.execute("UPDATE dhs_users SET last_used=? WHERE id=?", (now, user_id))
    return token


def get_user_from_token(token):
    if not token:
        return None
    now = _now()
    with _conn() as c:
        row = c.execute(
            """SELECT u.* FROM dhs_users u
               JOIN dhs_tokens t ON t.user_id = u.id
               WHERE t.token=? AND t.expires_at > ? AND u.status='approved' AND u.is_active=1""",
            (token, now)
        ).fetchone()
    return _user_dict(row)


def logout(token):
    with _conn() as c:
        c.execute("DELETE FROM dhs_tokens WHERE token=?", (token,))


# ── Password reset ────────────────────────────────────────────────────────────

def create_user_reset_token(user_id):
    token   = secrets.token_urlsafe(32)
    expires = (datetime.datetime.utcnow() +
               datetime.timedelta(seconds=_RESET_TOKEN_TTL)).isoformat()
    with _conn() as c:
        c.execute("DELETE FROM dhs_reset_tokens WHERE user_id=?", (user_id,))
        c.execute(
            "INSERT INTO dhs_reset_tokens (token, user_id, expires_at) VALUES (?,?,?)",
            (token, user_id, expires)
        )
    return token


def consume_user_reset_token(token, new_password):
    now = _now()
    with _conn() as c:
        row = c.execute(
            "SELECT user_id, expires_at FROM dhs_reset_tokens WHERE token=?", (token,)
        ).fetchone()
    if not row or now > row['expires_at']:
        return None
    with _conn() as c:
        c.execute("UPDATE dhs_users SET password=? WHERE id=?", (_hash_pw(new_password), row['user_id']))
        c.execute("DELETE FROM dhs_reset_tokens WHERE token=?", (token,))
    return row['user_id']


# ── Admin-initiated invites ────────────────────────────────────────────────────
# Lets an admin add a researcher directly instead of waiting for self-signup:
# generate a token pre-scoped to specific countries, hand the invitee the link,
# they set their own password and the account is approved+active immediately.

def create_invite(email, name, countries_allowed, created_by, notes=''):
    email = email.lower().strip()
    token   = 'inv_' + secrets.token_urlsafe(24)
    now     = _now()
    expires = (datetime.datetime.utcnow() +
               datetime.timedelta(days=_INVITE_TTL_DAYS)).isoformat()
    with _conn() as c:
        c.execute(
            '''INSERT INTO dhs_invites
               (token, email, name, countries_allowed, created_by, created_at, expires_at)
               VALUES (?,?,?,?,?,?,?)''',
            (token, email, name, json.dumps(countries_allowed), created_by, now, expires)
        )
    return token


def get_invite(token):
    with _conn() as c:
        row = c.execute("SELECT * FROM dhs_invites WHERE token=?", (token,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d['countries_allowed'] = json.loads(d['countries_allowed'] or '[]')
    return d


def invite_is_valid(invite):
    if not invite or invite.get('claimed_at'):
        return False
    return _now() <= invite['expires_at']


def claim_invite(token, password):
    """Create (or reactivate) an approved+active account from a valid invite.
    Returns the user dict, or None if the invite is missing/expired/claimed."""
    invite = get_invite(token)
    if not invite_is_valid(invite):
        return None

    existing = get_user_by_email(invite['email'])
    now = _now()
    with _conn() as c:
        if existing:
            user_id = existing['id']
            c.execute(
                '''UPDATE dhs_users
                   SET password=?, countries_allowed=?, status='approved', is_active=1,
                       reviewed_at=?, invited_by=?
                   WHERE id=?''',
                (_hash_pw(password), json.dumps(invite['countries_allowed']), now,
                 invite['created_by'], user_id)
            )
        else:
            user_id = secrets.token_hex(8)
            c.execute(
                '''INSERT INTO dhs_users
                   (id, name, email, password, countries_allowed, status, is_active,
                    invited_by, created_at, reviewed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (user_id, invite['name'] or invite['email'], invite['email'], _hash_pw(password),
                 json.dumps(invite['countries_allowed']), 'approved', 1,
                 invite['created_by'], now, now)
            )
        c.execute(
            "UPDATE dhs_invites SET claimed_at=?, claimed_user_id=? WHERE token=?",
            (now, user_id, token)
        )
    return get_user(user_id)


def list_invites(include_claimed=True):
    with _conn() as c:
        if include_claimed:
            rows = c.execute("SELECT * FROM dhs_invites ORDER BY created_at DESC").fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM dhs_invites WHERE claimed_at IS NULL ORDER BY created_at DESC"
            ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d['countries_allowed'] = json.loads(d['countries_allowed'] or '[]')
        result.append(d)
    return result


def revoke_invite(token):
    with _conn() as c:
        c.execute("DELETE FROM dhs_invites WHERE token=? AND claimed_at IS NULL", (token,))


init_db()
