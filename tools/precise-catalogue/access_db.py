"""
Access control database for PRECISE DuckDB requests.
SQLite-backed store for requests, approvals, and API keys.
"""
import sqlite3, secrets, json, datetime, os
import bcrypt as _bcrypt

DB_PATH = '/home/USER/PRECISE/access_control.db'

COUNTRIES = ['Kenya', 'Mozambique', 'Gambia']


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _hash_pw(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()


def _verify_pw(password: str, stored: str) -> bool:
    b = stored.encode() if isinstance(stored, str) else stored
    if not b.startswith(b'$2'):
        # Legacy plaintext — compare directly; caller should migrate on success
        return password == stored
    return _bcrypt.checkpw(password.encode(), b)


def init_db():
    with _conn() as c:
        c.executescript('''
            CREATE TABLE IF NOT EXISTS requests (
                id            TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                email         TEXT NOT NULL,
                institution   TEXT,
                hub_user      TEXT,
                purpose       TEXT,
                countries_req TEXT NOT NULL,
                status        TEXT DEFAULT 'pending',
                created_at    TEXT NOT NULL,
                reviewed_at   TEXT,
                notes         TEXT
            );
            CREATE TABLE IF NOT EXISTS api_keys (
                key              TEXT PRIMARY KEY,
                request_id       TEXT NOT NULL,
                name             TEXT NOT NULL,
                email            TEXT NOT NULL,
                countries        TEXT NOT NULL,
                created_at       TEXT NOT NULL,
                is_active        INTEGER DEFAULT 1,
                last_used        TEXT,
                FOREIGN KEY (request_id) REFERENCES requests(id)
            );
            CREATE TABLE IF NOT EXISTS catalogue_tokens (
                token      TEXT PRIMARY KEY,
                expires_at TEXT NOT NULL
            );
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
            CREATE TABLE IF NOT EXISTS catalogue_requests (
                id          TEXT PRIMARY KEY,
                catalogue   TEXT NOT NULL,
                name        TEXT NOT NULL,
                email       TEXT NOT NULL,
                institution TEXT,
                reason      TEXT,
                status      TEXT DEFAULT 'pending',
                created_at  TEXT NOT NULL,
                reviewed_at TEXT,
                notes       TEXT
            );
            CREATE TABLE IF NOT EXISTS shared_conversations (
                share_id   TEXT PRIMARY KEY,
                catalogue  TEXT NOT NULL,
                title      TEXT,
                messages   TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS catalogue_users (
                id           TEXT PRIMARY KEY,
                catalogue    TEXT NOT NULL,
                name         TEXT NOT NULL,
                email        TEXT NOT NULL,
                password     TEXT NOT NULL,
                token_budget INTEGER DEFAULT 1000000,
                tokens_used  INTEGER DEFAULT 0,
                is_active    INTEGER DEFAULT 1,
                status       TEXT DEFAULT 'approved',
                created_at   TEXT NOT NULL,
                last_used    TEXT,
                notes        TEXT
            );
            CREATE TABLE IF NOT EXISTS user_reset_tokens (
                token      TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS course_enrollments (
                id           TEXT PRIMARY KEY,
                course_id    TEXT NOT NULL,
                name         TEXT NOT NULL,
                email        TEXT NOT NULL,
                institution  TEXT,
                country      TEXT,
                status       TEXT DEFAULT 'pending',
                hub_username TEXT,
                created_at   TEXT NOT NULL,
                reviewed_at  TEXT,
                notes        TEXT
            );
            CREATE TABLE IF NOT EXISTS workshop_tokens (
                hub_username TEXT PRIMARY KEY,
                token_budget INTEGER DEFAULT 200000,
                tokens_used  INTEGER DEFAULT 0,
                created_at   TEXT NOT NULL,
                last_used    TEXT
            );
            CREATE TABLE IF NOT EXISTS workshop_survey_responses (
                id               TEXT PRIMARY KEY,
                submitted_at     TEXT NOT NULL,
                name             TEXT,
                email            TEXT,
                background       TEXT,
                overall_rating   INTEGER,
                modules_done     TEXT,
                confidence_pre   INTEGER,
                confidence_post  INTEGER,
                met_expectations TEXT,
                next_topic       TEXT,
                highlight        TEXT,
                improve          TEXT,
                recommend        TEXT
            );
            CREATE TABLE IF NOT EXISTS junior_course_progress (
                id           TEXT PRIMARY KEY,
                course_id    TEXT NOT NULL,
                session_id   TEXT NOT NULL,
                name         TEXT,
                email        TEXT,
                school       TEXT,
                quiz_score   INTEGER,
                quiz_total   INTEGER,
                modules_done INTEGER DEFAULT 0,
                status       TEXT DEFAULT 'started',
                started_at   TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(course_id, session_id)
            );
            CREATE TABLE IF NOT EXISTS junior_quiz_attempts (
                id             TEXT PRIMARY KEY,
                course_id      TEXT NOT NULL,
                session_id     TEXT NOT NULL,
                module_id      TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                score          INTEGER,
                total          INTEGER,
                passed         INTEGER DEFAULT 0,
                answers_json   TEXT,
                submitted_at   TEXT NOT NULL
            );
        ''')
        # Add user_id to catalogue_tokens if upgrading from an older schema
        try:
            c.execute("ALTER TABLE catalogue_tokens ADD COLUMN user_id TEXT DEFAULT NULL")
        except Exception:
            pass
        # Add new columns to existing admins table if upgrading
        for sql in [
            "ALTER TABLE admins ADD COLUMN email TEXT",
            "ALTER TABLE admins ADD COLUMN must_change_pw INTEGER DEFAULT 0",
        ]:
            try:
                c.execute(sql)
            except Exception:
                pass
        # Add status column to catalogue_users if upgrading
        try:
            c.execute("ALTER TABLE catalogue_users ADD COLUMN status TEXT DEFAULT 'approved'")
        except Exception:
            pass
        # Add password column to course_enrollments if upgrading
        try:
            c.execute("ALTER TABLE course_enrollments ADD COLUMN password TEXT")
        except Exception:
            pass
        # Add new survey columns if upgrading from earlier schema
        for sql in [
            "ALTER TABLE workshop_survey_responses ADD COLUMN met_expectations TEXT",
            "ALTER TABLE workshop_survey_responses ADD COLUMN next_topic TEXT",
        ]:
            try:
                c.execute(sql)
            except Exception:
                pass
    _seed_admins()


# ── Requests ──────────────────────────────────────────────────────────────────

def create_request(name, email, institution, hub_user, purpose, countries):
    req_id = secrets.token_hex(8)
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute(
            '''INSERT INTO requests
               (id, name, email, institution, hub_user, purpose, countries_req, created_at)
               VALUES (?,?,?,?,?,?,?,?)''',
            (req_id, name, email, institution, hub_user, purpose,
             json.dumps(countries), now)
        )
    return req_id


def get_requests(status=None):
    with _conn() as c:
        if status:
            rows = c.execute(
                "SELECT * FROM requests WHERE status=? ORDER BY created_at DESC",
                (status,)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM requests ORDER BY created_at DESC"
            ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d['countries_req'] = json.loads(d['countries_req'])
        result.append(d)
    return result


def get_request(req_id):
    with _conn() as c:
        r = c.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d['countries_req'] = json.loads(d['countries_req'])
    return d


def get_request_with_key(req_id):
    d = get_request(req_id)
    if not d:
        return None
    if d['status'] == 'approved':
        with _conn() as c:
            k = c.execute(
                "SELECT key, countries FROM api_keys WHERE request_id=? AND is_active=1",
                (req_id,)
            ).fetchone()
        if k:
            d['api_key'] = k['key']
            d['countries'] = json.loads(k['countries'])
    return d


# ── Approvals / rejections ────────────────────────────────────────────────────

def approve_request(req_id, countries_allowed, notes=''):
    """Returns (api_key, request_dict) or (None, None) if not found."""
    req = get_request(req_id)
    if not req:
        return None, None
    # Guard against duplicate approvals (e.g. double-click)
    with _conn() as c:
        existing = c.execute(
            "SELECT key FROM api_keys WHERE request_id=? AND is_active=1",
            (req_id,)
        ).fetchone()
        if existing:
            req['status'] = 'approved'
            return existing['key'], req
    key = 'pals_' + secrets.token_urlsafe(30)
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute(
            "UPDATE requests SET status='approved', reviewed_at=?, notes=? WHERE id=?",
            (now, notes, req_id)
        )
        c.execute(
            '''INSERT INTO api_keys
               (key, request_id, name, email, countries, created_at)
               VALUES (?,?,?,?,?,?)''',
            (key, req_id, req['name'], req['email'],
             json.dumps(countries_allowed), now)
        )
    req['status'] = 'approved'
    return key, req


def reject_request(req_id, notes=''):
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute(
            "UPDATE requests SET status='rejected', reviewed_at=?, notes=? WHERE id=?",
            (now, notes, req_id)
        )


# ── API key validation (called by api.py) ────────────────────────────────────

def validate_key(api_key):
    """Returns {'name', 'email', 'countries': [...]} or None."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM api_keys WHERE key=? AND is_active=1",
            (api_key,)
        ).fetchone()
        if row:
            c.execute(
                "UPDATE api_keys SET last_used=? WHERE key=?",
                (datetime.datetime.utcnow().isoformat(), api_key)
            )
            return {
                'name':      row['name'],
                'email':     row['email'],
                'countries': json.loads(row['countries']),
            }
    return None


def revoke_access(req_id):
    """Deactivate all keys for a request and mark it revoked."""
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute("UPDATE api_keys  SET is_active=0           WHERE request_id=?", (req_id,))
        c.execute("UPDATE requests  SET status='revoked', reviewed_at=? WHERE id=?", (now, req_id))


def revoke_key(api_key):
    with _conn() as c:
        c.execute("UPDATE api_keys SET is_active=0 WHERE key=?", (api_key,))


# ── Admin accounts ────────────────────────────────────────────────────────────
# Multi-user admin table in SQLite. Seed runs once; use set_admin_password()
# to update a user's password, or add_admin() to create a new one.

_PASS_FILE = '/home/USER/PRECISE/.duck_admin_pass'

_INITIAL_ADMINS = {
    'rutendo': {'password': None, 'email': 'admin@example.org',    'must_change_pw': 0},
    'zororo':  {'password': None, 'email': 'admin@example.org','must_change_pw': 1},
    'bongani': {'password': None, 'email': 'admin@example.org',        'must_change_pw': 1},
}


def _seed_admins():
    """Insert default admin rows only if they don't already exist; patch emails on existing rows."""
    legacy_pass = open(_PASS_FILE).read().strip() if os.path.exists(_PASS_FILE) else 'precise-admin'
    with _conn() as c:
        for username, info in _INITIAL_ADMINS.items():
            existing = c.execute(
                "SELECT 1 FROM admins WHERE username=?", (username,)
            ).fetchone()
            if not existing:
                raw_pw = info['password'] if info['password'] is not None else legacy_pass
                c.execute(
                    "INSERT INTO admins (username, password, email, must_change_pw) VALUES (?,?,?,?)",
                    (username, _hash_pw(raw_pw), info['email'], info['must_change_pw'])
                )
            else:
                # Patch email in if the column was just added
                c.execute(
                    "UPDATE admins SET email=? WHERE username=? AND (email IS NULL OR email='')",
                    (info['email'], username)
                )


def check_admin(username, password):
    """Return (True, must_change_pw) if credentials match, else (False, False)."""
    with _conn() as c:
        row = c.execute(
            "SELECT password, must_change_pw FROM admins WHERE username=?", (username,)
        ).fetchone()
    if not row or not _verify_pw(password, row['password']):
        return False, False
    # Migrate legacy plaintext to bcrypt on first successful login
    if not row['password'].startswith('$2'):
        with _conn() as c:
            c.execute("UPDATE admins SET password=? WHERE username=?",
                      (_hash_pw(password), username))
    return True, bool(row['must_change_pw'])


def get_admin_email(username):
    with _conn() as c:
        row = c.execute("SELECT email FROM admins WHERE username=?", (username,)).fetchone()
    return row['email'] if row else None


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


# ── Admin password-reset tokens ───────────────────────────────────────────────

_TOKEN_TTL_ADMIN = 3600  # 1 hour


def create_admin_reset_token(username):
    """Store a timed reset token for username, return the token string."""
    token = secrets.token_urlsafe(32)
    expires = (datetime.datetime.utcnow() +
               datetime.timedelta(seconds=_TOKEN_TTL_ADMIN)).isoformat()
    with _conn() as c:
        # Remove any stale tokens for this user
        c.execute("DELETE FROM admin_reset_tokens WHERE username=?", (username,))
        c.execute(
            "INSERT INTO admin_reset_tokens (token, username, expires_at) VALUES (?,?,?)",
            (token, username, expires)
        )
    return token


def verify_admin_reset_token(token):
    """Return username if token is valid and not expired, else None."""
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        row = c.execute(
            "SELECT username, expires_at FROM admin_reset_tokens WHERE token=?", (token,)
        ).fetchone()
    if not row:
        return None
    if now > row['expires_at']:
        with _conn() as c:
            c.execute("DELETE FROM admin_reset_tokens WHERE token=?", (token,))
        return None
    return row['username']


def consume_admin_reset_token(token, new_password):
    """Set new password if token valid. Returns username on success, None on failure."""
    username = verify_admin_reset_token(token)
    if not username:
        return None
    set_admin_password(username, new_password)
    with _conn() as c:
        c.execute("DELETE FROM admin_reset_tokens WHERE token=?", (token,))
    return username


# ── Catalogue session tokens (SQLite-backed, survive restarts) ────────────────

_TOKEN_TTL = 8 * 3600  # seconds

def issue_catalogue_token() -> str:
    token = 'cat_' + secrets.token_urlsafe(32)
    expires = (datetime.datetime.utcnow() +
               datetime.timedelta(seconds=_TOKEN_TTL)).isoformat()
    with _conn() as c:
        c.execute("DELETE FROM catalogue_tokens WHERE expires_at < ?",
                  (datetime.datetime.utcnow().isoformat(),))
        c.execute("INSERT INTO catalogue_tokens (token, expires_at) VALUES (?, ?)",
                  (token, expires))
    return token


def validate_catalogue_token(token: str) -> bool:
    if not token:
        return False
    with _conn() as c:
        row = c.execute(
            "SELECT expires_at FROM catalogue_tokens WHERE token=?", (token,)
        ).fetchone()
    if not row:
        return False
    if datetime.datetime.utcnow().isoformat() > row['expires_at']:
        with _conn() as c:
            c.execute("DELETE FROM catalogue_tokens WHERE token=?", (token,))
        return False
    return True


# ── Catalogue user accounts ───────────────────────────────────────────────────

def create_catalogue_user(catalogue, name, email, password, token_budget=1_000_000, notes=''):
    user_id = secrets.token_hex(8)
    now     = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute(
            '''INSERT OR IGNORE INTO catalogue_users
               (id, catalogue, name, email, password, token_budget, created_at, notes)
               VALUES (?,?,?,?,?,?,?,?)''',
            (user_id, catalogue, name, email.lower().strip(), _hash_pw(password), token_budget, now, notes)
        )
    return user_id


def authenticate_catalogue_user(catalogue, email, password):
    """Return user dict if credentials match and account is approved, else None."""
    with _conn() as c:
        row = c.execute(
            """SELECT * FROM catalogue_users
               WHERE catalogue=? AND email=?
                 AND (status IS NULL OR status='approved') AND is_active=1""",
            (catalogue, email.lower().strip())
        ).fetchone()
    if not row:
        return None
    user = dict(row)
    if not _verify_pw(password, user['password']):
        return None
    # Migrate legacy plaintext to bcrypt on first successful login
    if not user['password'].startswith('$2'):
        with _conn() as c:
            c.execute("UPDATE catalogue_users SET password=? WHERE id=?",
                      (_hash_pw(password), user['id']))
    return user


def get_catalogue_user(user_id):
    with _conn() as c:
        row = c.execute("SELECT * FROM catalogue_users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_catalogue_user_from_token(token):
    """Return the catalogue user linked to a valid (non-expired) session token."""
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        row = c.execute(
            """SELECT cu.* FROM catalogue_users cu
               JOIN catalogue_tokens ct ON ct.user_id = cu.id
               WHERE ct.token=? AND ct.expires_at > ?""",
            (token, now)
        ).fetchone()
    return dict(row) if row else None


def get_catalogue_users(catalogue=None):
    with _conn() as c:
        if catalogue:
            rows = c.execute(
                "SELECT * FROM catalogue_users WHERE catalogue=? ORDER BY created_at DESC",
                (catalogue,)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM catalogue_users ORDER BY catalogue, created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def update_catalogue_user_budget(user_id, new_budget):
    with _conn() as c:
        c.execute("UPDATE catalogue_users SET token_budget=? WHERE id=?", (new_budget, user_id))


def revoke_catalogue_user(user_id):
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute("UPDATE catalogue_users SET is_active=0 WHERE id=?", (user_id,))
        c.execute("DELETE FROM catalogue_tokens WHERE user_id=?", (user_id,))


def reinstate_catalogue_user(user_id):
    with _conn() as c:
        c.execute("UPDATE catalogue_users SET is_active=1 WHERE id=?", (user_id,))


def add_tokens_used(user_id, tokens):
    """Atomically add to a user's tokens_used counter. Returns new total."""
    with _conn() as c:
        c.execute(
            "UPDATE catalogue_users SET tokens_used=tokens_used+?, last_used=? WHERE id=?",
            (tokens, datetime.datetime.utcnow().isoformat(), user_id)
        )
        row = c.execute("SELECT tokens_used FROM catalogue_users WHERE id=?", (user_id,)).fetchone()
    return row['tokens_used'] if row else 0


def get_or_create_workshop_user(hub_username, default_budget=200_000):
    """Look up a workshop student's token budget row, creating one on first use."""
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM workshop_tokens WHERE hub_username=?", (hub_username,)
        ).fetchone()
        if row is None:
            c.execute(
                "INSERT INTO workshop_tokens (hub_username, token_budget, tokens_used, created_at) "
                "VALUES (?, ?, 0, ?)",
                (hub_username, default_budget, now)
            )
            row = c.execute(
                "SELECT * FROM workshop_tokens WHERE hub_username=?", (hub_username,)
            ).fetchone()
    return dict(row)


def add_workshop_tokens_used(hub_username, tokens):
    """Atomically add to a workshop student's tokens_used counter. Returns new total."""
    with _conn() as c:
        c.execute(
            "UPDATE workshop_tokens SET tokens_used=tokens_used+?, last_used=? WHERE hub_username=?",
            (tokens, datetime.datetime.utcnow().isoformat(), hub_username)
        )
        row = c.execute(
            "SELECT tokens_used FROM workshop_tokens WHERE hub_username=?", (hub_username,)
        ).fetchone()
    return row['tokens_used'] if row else 0


def list_workshop_users():
    with _conn() as c:
        rows = c.execute("SELECT * FROM workshop_tokens ORDER BY tokens_used DESC").fetchall()
    return [dict(r) for r in rows]


def update_workshop_user_budget(hub_username, new_budget):
    with _conn() as c:
        c.execute(
            "UPDATE workshop_tokens SET token_budget=? WHERE hub_username=?",
            (new_budget, hub_username)
        )


def issue_user_catalogue_token(user_id):
    """Issue a session token linked to a specific user."""
    token   = 'cat_' + secrets.token_urlsafe(32)
    expires = (datetime.datetime.utcnow() +
               datetime.timedelta(seconds=_TOKEN_TTL)).isoformat()
    now     = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute("DELETE FROM catalogue_tokens WHERE expires_at < ?", (now,))
        c.execute(
            "INSERT INTO catalogue_tokens (token, expires_at, user_id) VALUES (?,?,?)",
            (token, expires, user_id)
        )
        c.execute("UPDATE catalogue_users SET last_used=? WHERE id=?", (now, user_id))
    return token


# ── Catalogue access requests ─────────────────────────────────────────────────

def create_catalogue_request(catalogue, name, email, institution, reason):
    req_id = secrets.token_hex(8)
    now    = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute(
            '''INSERT INTO catalogue_requests
               (id, catalogue, name, email, institution, reason, created_at)
               VALUES (?,?,?,?,?,?,?)''',
            (req_id, catalogue, name, email, institution, reason, now)
        )
    return req_id


def get_catalogue_requests(catalogue=None, status=None):
    with _conn() as c:
        if catalogue and status:
            rows = c.execute(
                "SELECT * FROM catalogue_requests WHERE catalogue=? AND status=? ORDER BY created_at DESC",
                (catalogue, status)
            ).fetchall()
        elif catalogue:
            rows = c.execute(
                "SELECT * FROM catalogue_requests WHERE catalogue=? ORDER BY created_at DESC",
                (catalogue,)
            ).fetchall()
        elif status:
            rows = c.execute(
                "SELECT * FROM catalogue_requests WHERE status=? ORDER BY created_at DESC",
                (status,)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM catalogue_requests ORDER BY created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def approve_catalogue_request(req_id, notes=''):
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        r = c.execute("SELECT * FROM catalogue_requests WHERE id=?", (req_id,)).fetchone()
        if not r:
            return None
        c.execute(
            "UPDATE catalogue_requests SET status='approved', reviewed_at=?, notes=? WHERE id=?",
            (now, notes, req_id)
        )
    return dict(r)


def reject_catalogue_request(req_id, notes=''):
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        r = c.execute("SELECT * FROM catalogue_requests WHERE id=?", (req_id,)).fetchone()
        if not r:
            return None
        c.execute(
            "UPDATE catalogue_requests SET status='rejected', reviewed_at=?, notes=? WHERE id=?",
            (now, notes, req_id)
        )
    return dict(r)


# ── Shared conversations ──────────────────────────────────────────────────────

def create_shared_conversation(catalogue, title, messages):
    share_id = 'sh_' + secrets.token_urlsafe(10)
    now      = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute(
            "INSERT INTO shared_conversations (share_id, catalogue, title, messages, created_at) VALUES (?,?,?,?,?)",
            (share_id, catalogue, title, json.dumps(messages), now)
        )
    return share_id


def get_shared_conversation(share_id):
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM shared_conversations WHERE share_id=?", (share_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d['messages'] = json.loads(d['messages'])
    return d


# ── Catalogue user self-signup (precise / he2at) ─────────────────────────────

def create_catalogue_signup_user(catalogue, name, email, password, institution='', reason=''):
    """Create a catalogue user from a self-signup — status='pending', is_active=0.
    Returns (user_id, None) on success or (None, existing_status) if email exists."""
    email = email.lower().strip()
    with _conn() as c:
        existing = c.execute(
            "SELECT id, status FROM catalogue_users WHERE catalogue=? AND email=?",
            (catalogue, email)
        ).fetchone()
        if existing:
            return None, existing['status']
    user_id = secrets.token_hex(8)
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute(
            '''INSERT INTO catalogue_users
               (id, catalogue, name, email, password, token_budget, is_active, status, created_at, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (user_id, catalogue, name, email, _hash_pw(password), 1_000_000, 0, 'pending', now,
             f'institution={institution};reason={reason}')
        )
    return user_id, None


def get_catalogue_user_by_email(catalogue, email):
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM catalogue_users WHERE catalogue=? AND email=?",
            (catalogue, email.lower().strip())
        ).fetchone()
    return dict(row) if row else None


def approve_catalogue_signup_user(user_id):
    with _conn() as c:
        c.execute(
            "UPDATE catalogue_users SET status='approved', is_active=1 WHERE id=?",
            (user_id,)
        )
        row = c.execute("SELECT * FROM catalogue_users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def reject_catalogue_signup_user(user_id):
    with _conn() as c:
        c.execute(
            "UPDATE catalogue_users SET status='rejected', is_active=0 WHERE id=?",
            (user_id,)
        )
        row = c.execute("SELECT * FROM catalogue_users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


# ── Portal user accounts (public sign-up + admin approval) ───────────────────

def create_portal_user(name, email, password, institution='', purpose=''):
    """Create a portal signup request — status='pending', is_active=0.
    Returns (user_id, None) on success or (None, existing_status) if email exists."""
    email = email.lower().strip()
    with _conn() as c:
        existing = c.execute(
            "SELECT id, status FROM catalogue_users WHERE catalogue='portal' AND email=?",
            (email,)
        ).fetchone()
        if existing:
            return None, existing['status']
    user_id = secrets.token_hex(8)
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute(
            '''INSERT INTO catalogue_users
               (id, catalogue, name, email, password, token_budget, is_active, status, created_at, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (user_id, 'portal', name, email, _hash_pw(password), 0, 0, 'pending', now,
             f'institution={institution};purpose={purpose}')
        )
    return user_id, None


def get_portal_user_by_email(email):
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM catalogue_users WHERE catalogue='portal' AND email=?",
            (email.lower().strip(),)
        ).fetchone()
    return dict(row) if row else None


def get_portal_users(status=None):
    with _conn() as c:
        if status:
            rows = c.execute(
                "SELECT * FROM catalogue_users WHERE catalogue='portal' AND status=? ORDER BY created_at DESC",
                (status,)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM catalogue_users WHERE catalogue='portal' ORDER BY created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def authenticate_portal_user(email, password):
    """Authenticate a portal user — only succeeds if status='approved' and is_active=1."""
    with _conn() as c:
        row = c.execute(
            """SELECT * FROM catalogue_users
               WHERE catalogue='portal' AND email=?
                 AND status='approved' AND is_active=1""",
            (email.lower().strip(),)
        ).fetchone()
    if not row:
        return None
    user = dict(row)
    if not _verify_pw(password, user['password']):
        return None
    # Migrate legacy plaintext to bcrypt on first successful login
    if not user['password'].startswith('$2'):
        with _conn() as c:
            c.execute("UPDATE catalogue_users SET password=? WHERE id=?",
                      (_hash_pw(password), user['id']))
    return user


def approve_portal_user(user_id):
    with _conn() as c:
        c.execute(
            "UPDATE catalogue_users SET status='approved', is_active=1 WHERE id=? AND catalogue='portal'",
            (user_id,)
        )
        row = c.execute("SELECT * FROM catalogue_users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def reject_portal_user(user_id):
    with _conn() as c:
        c.execute(
            "UPDATE catalogue_users SET status='rejected', is_active=0 WHERE id=? AND catalogue='portal'",
            (user_id,)
        )
        row = c.execute("SELECT * FROM catalogue_users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def revoke_portal_user(user_id):
    with _conn() as c:
        c.execute(
            "UPDATE catalogue_users SET status='revoked', is_active=0 WHERE id=? AND catalogue='portal'",
            (user_id,)
        )
        c.execute("DELETE FROM catalogue_tokens WHERE user_id=?", (user_id,))
        row = c.execute("SELECT * FROM catalogue_users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def reinstate_portal_user(user_id):
    with _conn() as c:
        c.execute(
            "UPDATE catalogue_users SET status='approved', is_active=1 WHERE id=? AND catalogue='portal'",
            (user_id,)
        )
        row = c.execute("SELECT * FROM catalogue_users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def portal_logout(token):
    with _conn() as c:
        c.execute("DELETE FROM catalogue_tokens WHERE token=?", (token,))


# ── Portal password-reset tokens ──────────────────────────────────────────────

_TOKEN_TTL_USER_RESET = 3600  # 1 hour


def create_user_reset_token(user_id):
    token = secrets.token_urlsafe(32)
    expires = (datetime.datetime.utcnow() +
               datetime.timedelta(seconds=_TOKEN_TTL_USER_RESET)).isoformat()
    with _conn() as c:
        c.execute("DELETE FROM user_reset_tokens WHERE user_id=?", (user_id,))
        c.execute(
            "INSERT INTO user_reset_tokens (token, user_id, expires_at) VALUES (?,?,?)",
            (token, user_id, expires)
        )
    return token


def verify_user_reset_token(token):
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        row = c.execute(
            "SELECT user_id, expires_at FROM user_reset_tokens WHERE token=?", (token,)
        ).fetchone()
    if not row:
        return None
    if now > row['expires_at']:
        with _conn() as c:
            c.execute("DELETE FROM user_reset_tokens WHERE token=?", (token,))
        return None
    return row['user_id']


def consume_user_reset_token(token, new_password):
    """Set new password if token valid. Returns user_id on success, None on failure."""
    user_id = verify_user_reset_token(token)
    if not user_id:
        return None
    with _conn() as c:
        c.execute("UPDATE catalogue_users SET password=? WHERE id=?",
                  (_hash_pw(new_password), user_id))
        c.execute("DELETE FROM user_reset_tokens WHERE token=?", (token,))
    return user_id




# ── Course enrollment ─────────────────────────────────────────────────────────

def create_course_enrollment(course_id, name, email, institution, country, password):
    """Store a course enrollment request. Returns (enrol_id, None) or (None, existing_status).

    The chosen password is held in plaintext only until the enrolment is approved —
    it must be handed to JupyterHub's own NativeAuthenticator (which does its own
    bcrypt hashing) at approval time; see approve_course_enrollment. It is kept
    (not cleared) on rejection so a mistaken rejection can be undone via
    reopen_course_enrollment without forcing the applicant to resubmit. A fresh
    submission against a rejected row reopens that same row as pending rather
    than being blocked; pending/approved rows still block duplicate submission."""
    email = email.lower().strip()
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        existing = c.execute(
            "SELECT id, status FROM course_enrollments WHERE course_id=? AND email=?",
            (course_id, email)
        ).fetchone()
        if existing:
            if existing['status'] != 'rejected':
                return None, existing['status']
            # A previously rejected applicant is allowed to resubmit — reopen
            # the same record as a fresh pending request rather than blocking.
            c.execute(
                '''UPDATE course_enrollments
                   SET name=?, institution=?, country=?, password=?, status='pending',
                       hub_username=NULL, created_at=?, reviewed_at=NULL, notes=NULL
                   WHERE id=?''',
                (name, institution, country, password, now, existing['id'])
            )
            return existing['id'], None
    enrol_id = secrets.token_hex(8)
    with _conn() as c:
        c.execute(
            '''INSERT INTO course_enrollments
               (id, course_id, name, email, institution, country, password, created_at)
               VALUES (?,?,?,?,?,?,?,?)''',
            (enrol_id, course_id, name, email, institution, country, password, now)
        )
    return enrol_id, None


def get_course_enrollments(status=None, course_id=None):
    with _conn() as c:
        q = "SELECT id, course_id, name, email, institution, country, status, hub_username, created_at, reviewed_at, notes FROM course_enrollments WHERE 1=1"
        params = []
        if status:
            q += " AND status=?"
            params.append(status)
        if course_id:
            q += " AND course_id=?"
            params.append(course_id)
        q += " ORDER BY created_at DESC"
        rows = c.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def get_course_enrollment_count(course_id, status='approved'):
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM course_enrollments WHERE course_id=? AND status=?",
            (course_id, status)
        ).fetchone()
    return row['n'] if row else 0


def get_course_enrollment_by_email(course_id, email):
    email = email.lower().strip()
    with _conn() as c:
        row = c.execute(
            "SELECT id, course_id, name, email, status, hub_username FROM course_enrollments WHERE course_id=? AND email=?",
            (course_id, email)
        ).fetchone()
    return dict(row) if row else None


def get_course_enrollment(enrol_id):
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM course_enrollments WHERE id=?", (enrol_id,)
        ).fetchone()
    return dict(row) if row else None


def approve_course_enrollment(enrol_id, hub_username):
    """Mark approved and clear the stored plaintext password — it has already
    been handed to JupyterHub's NativeAuthenticator by this point."""
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute(
            "UPDATE course_enrollments SET status='approved', hub_username=?, reviewed_at=?, password=NULL WHERE id=?",
            (hub_username, now, enrol_id)
        )
        row = c.execute(
            "SELECT id, course_id, name, email, institution, country, status, hub_username, created_at, reviewed_at, notes FROM course_enrollments WHERE id=?",
            (enrol_id,)
        ).fetchone()
    return dict(row) if row else None


def reject_course_enrollment(enrol_id, notes=''):
    """Mark rejected. The password is deliberately kept (not nulled) so the
    admin can undo a mistaken rejection via reopen_course_enrollment() without
    forcing the applicant to resubmit."""
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute(
            "UPDATE course_enrollments SET status='rejected', reviewed_at=?, notes=? WHERE id=?",
            (now, notes, enrol_id)
        )
        row = c.execute(
            "SELECT id, course_id, name, email, institution, country, status, hub_username, created_at, reviewed_at, notes FROM course_enrollments WHERE id=?",
            (enrol_id,)
        ).fetchone()
    return dict(row) if row else None


def reopen_course_enrollment(enrol_id):
    """Undo a rejection — reset a rejected enrolment back to pending so the
    admin can reconsider it. Returns None if the enrolment isn't rejected."""
    with _conn() as c:
        row = c.execute("SELECT status FROM course_enrollments WHERE id=?", (enrol_id,)).fetchone()
        if not row or row['status'] not in ('rejected', 'revoked'):
            return None
        c.execute(
            "UPDATE course_enrollments SET status='pending', reviewed_at=NULL, notes=NULL, hub_username=NULL WHERE id=?",
            (enrol_id,)
        )
        row = c.execute(
            "SELECT id, course_id, name, email, institution, country, status, hub_username, created_at, reviewed_at, notes FROM course_enrollments WHERE id=?",
            (enrol_id,)
        ).fetchone()
    return dict(row) if row else None


def revoke_course_enrollment(enrol_id):
    """Mark an approved enrolment as revoked. Returns the record (with hub_username
    so the caller can delete the hub account), or None if not found / not approved."""
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        row = c.execute("SELECT status, hub_username FROM course_enrollments WHERE id=?", (enrol_id,)).fetchone()
        if not row or row['status'] != 'approved':
            return None
        c.execute(
            "UPDATE course_enrollments SET status='revoked', reviewed_at=? WHERE id=?",
            (now, enrol_id)
        )
        row = c.execute(
            "SELECT id, course_id, name, email, institution, country, status, hub_username, created_at, reviewed_at, notes FROM course_enrollments WHERE id=?",
            (enrol_id,)
        ).fetchone()
    return dict(row) if row else None


def get_course_student_hub_usernames():
    """Return the set of hub_username values for all approved course enrolments.
    Used to filter course students out of the general hub users listing."""
    with _conn() as c:
        rows = c.execute(
            "SELECT hub_username FROM course_enrollments WHERE status='approved' AND hub_username IS NOT NULL"
        ).fetchall()
    return {r['hub_username'] for r in rows}


# ── Workshop survey ───────────────────────────────────────────────────────────

def create_survey_response(name, email, background, overall_rating,
                            modules_done, confidence_pre, confidence_post,
                            met_expectations, next_topic,
                            highlight, improve, recommend):
    resp_id = secrets.token_hex(8)
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        c.execute(
            '''INSERT INTO workshop_survey_responses
               (id, submitted_at, name, email, background, overall_rating,
                modules_done, confidence_pre, confidence_post,
                met_expectations, next_topic, highlight, improve, recommend)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (resp_id, now, name or None, email or None, background or None,
             overall_rating or None, json.dumps(modules_done) if modules_done else None,
             confidence_pre or None, confidence_post or None,
             met_expectations or None, next_topic or None,
             highlight or None, improve or None, recommend or None)
        )
    return resp_id


def get_survey_responses():
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM workshop_survey_responses ORDER BY submitted_at DESC"
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if d.get('modules_done'):
            try:
                d['modules_done'] = json.loads(d['modules_done'])
            except Exception:
                d['modules_done'] = []
        else:
            d['modules_done'] = []
        result.append(d)
    return result


def create_or_update_junior_progress(course_id, session_id, name=None, email=None, school=None,
                                      quiz_score=None, quiz_total=None, modules_done=None, status=None):
    """
    Upserts by (course_id, session_id) so a learner's row updates in place as they
    progress, instead of creating a new row per event.
    """
    now = datetime.datetime.utcnow().isoformat()
    with _conn() as c:
        row = c.execute(
            "SELECT id FROM junior_course_progress WHERE course_id=? AND session_id=?",
            (course_id, session_id)
        ).fetchone()
        if row:
            fields, vals = [], []
            for col, val in (('name', name), ('email', email), ('school', school),
                              ('quiz_score', quiz_score), ('quiz_total', quiz_total),
                              ('modules_done', modules_done)):
                if val is not None:
                    fields.append(f"{col}=?")
                    vals.append(val)
            if status is not None:
                fields.append("status=?")
                vals.append(status)
                if status == 'completed':
                    fields.append("completed_at=?")
                    vals.append(now)
            if fields:
                vals.append(row['id'])
                c.execute(f"UPDATE junior_course_progress SET {', '.join(fields)} WHERE id=?", vals)
            return row['id']
        else:
            pid = secrets.token_hex(8)
            c.execute(
                '''INSERT INTO junior_course_progress
                   (id, course_id, session_id, name, email, school, quiz_score, quiz_total,
                    modules_done, status, started_at, completed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (pid, course_id, session_id, name, email, school, quiz_score, quiz_total,
                 modules_done or 0, status or 'started', now, now if status == 'completed' else None)
            )
            return pid


def get_junior_progress(course_id=None):
    with _conn() as c:
        if course_id:
            rows = c.execute(
                "SELECT * FROM junior_course_progress WHERE course_id=? ORDER BY started_at DESC",
                (course_id,)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM junior_course_progress ORDER BY started_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def get_junior_progress_count(course_id):
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM junior_course_progress WHERE course_id=?",
            (course_id,)
        ).fetchone()
    return row['n'] if row else 0



def create_quiz_attempt(course_id, session_id, module_id, attempt_number, score=None, total=None,
                         passed=False, answers=None):
    """Logs a single quiz-submission attempt for one module. One row per Submit click,
    so the admin view can see exactly how many tries a question set took."""
    now = datetime.datetime.utcnow().isoformat()
    aid = secrets.token_hex(8)
    answers_json = json.dumps(answers) if answers is not None else None
    with _conn() as c:
        c.execute(
            '''INSERT INTO junior_quiz_attempts
               (id, course_id, session_id, module_id, attempt_number, score, total, passed,
                answers_json, submitted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (aid, course_id, session_id, module_id, attempt_number, score, total,
             1 if passed else 0, answers_json, now)
        )
    return aid


def get_quiz_attempts(course_id=None, session_id=None):
    q = "SELECT * FROM junior_quiz_attempts WHERE 1=1"
    params = []
    if course_id:
        q += " AND course_id=?"
        params.append(course_id)
    if session_id:
        q += " AND session_id=?"
        params.append(session_id)
    q += " ORDER BY submitted_at ASC"
    with _conn() as c:
        rows = c.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def get_quiz_attempt_summary(course_id):
    """Per (session_id, module_id): number of attempts taken and the attempt_number on
    which the student first passed (None if never passed yet)."""
    with _conn() as c:
        rows = c.execute(
            "SELECT session_id, module_id, attempt_number, passed FROM junior_quiz_attempts "
            "WHERE course_id=? ORDER BY attempt_number ASC",
            (course_id,)
        ).fetchall()
    summary = {}
    for r in rows:
        key = (r['session_id'], r['module_id'])
        s = summary.setdefault(key, {'attempts': 0, 'passed_on': None})
        s['attempts'] += 1
        if r['passed'] and s['passed_on'] is None:
            s['passed_on'] = r['attempt_number']
    return summary



init_db()
