import sqlite3
import bcrypt
import secrets
import smtplib
import os
import logging
from email.mime.text import MIMEText
from datetime import datetime, timedelta

logger = logging.getLogger("palsearth.auth")

DB_PATH = '/home/USER/PRECISE/palsearth/palsearth_users.db'
SMTP_FROM = 'admin@example.org'
SMTP_PASS = os.environ.get('SMTP_APP_PASSWORD', '')
ADMIN_EMAIL = 'admin@example.org'
PALSEARTH_URL = 'https://portal.placealert.org/palsearth/'
RESET_TOKEN_TTL_SECONDS = 3600  # 1 hour, matching the catalogue site's reset flow
SESSION_TOKEN_TTL_SECONDS = 30 * 24 * 3600  # 30 days — keeps you signed in across browser refreshes
# Approve/deny happens on the unified admin dashboard (PALs Earth — Accounts
# panel there), not inside this app — see duckdb_request.py.
PENDING_URL = 'https://portal.placealert.org/duckrequest/admin'


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            email         TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            is_approved   INTEGER DEFAULT 0,
            role          TEXT DEFAULT 'user',
            created_at    TEXT DEFAULT (datetime('now')),
            last_login    TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS session_tokens (
            token      TEXT PRIMARY KEY,
            username   TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            token      TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            expires_at TEXT NOT NULL
        )
    """)
    # Added 2026-10-01 for the admin approve/deny pending-signups page.
    # sqlite has no "ADD COLUMN IF NOT EXISTS", so probe and migrate once.
    existing_cols = {row["name"] for row in c.execute("PRAGMA table_info(users)")}
    if "is_denied" not in existing_cols:
        c.execute("ALTER TABLE users ADD COLUMN is_denied INTEGER DEFAULT 0")
    # Added 2026-10-08 for the internal Cohort Exposure Assignment section —
    # cohort_access gates who sees/uses it (deliberately narrower than "has
    # a PALs Earth account", since this is IRB-sensitive cohort data, not
    # public GEE extraction); hub_username is which PALSlab Hub account's
    # ~/CohortExposures/ folder a finished job gets delivered into.
    if "cohort_access" not in existing_cols:
        c.execute("ALTER TABLE users ADD COLUMN cohort_access INTEGER DEFAULT 0")
    if "hub_username" not in existing_cols:
        c.execute("ALTER TABLE users ADD COLUMN hub_username TEXT")
    # Added 2026-10-08 (same day, follow-up): cohort_access alone was too
    # coarse — it handed a granted account the WHOLE multi-country cohort.
    # cohort_countries is a comma-separated allowlist (e.g. "Kenya,Gambia")
    # of which country/site(s) that account may query; empty/NULL means NO
    # countries yet (safe default — an admin must explicitly scope someone
    # in, not opt them out of a restriction).
    if "cohort_countries" not in existing_cols:
        c.execute("ALTER TABLE users ADD COLUMN cohort_countries TEXT")
    # Added 2026-10-14 for the DHS cohort: cohort_countries above only ever
    # covered one cohort ("precise") — a real multi-cohort tool needs each
    # account scoped per cohort independently (someone might have PRECISE
    # access to all 3 of its countries but no DHS access at all, or DHS
    # access to a totally different country set). This table replaces
    # cohort_countries going forward; that column is left in place
    # (untouched, not dropped) rather than migrated away entirely.
    c.execute("""
        CREATE TABLE IF NOT EXISTS cohort_country_access (
            username   TEXT NOT NULL,
            cohort_key TEXT NOT NULL,
            country    TEXT NOT NULL,
            PRIMARY KEY (username, cohort_key, country)
        )
    """)
    # One-time (idempotent) migration: copy the old flat cohort_countries
    # column into the new table under cohort_key='precise', since that's
    # the only cohort that existed when it was populated. Clears the legacy
    # column on each user once migrated, rather than checking for an
    # existing row in cohort_country_access -- an admin later revoking a
    # user down to zero PRECISE countries would otherwise look identical to
    # "never migrated" on the next _conn() call and get silently
    # resurrected from the stale column (found 2026-10-14 testing the
    # Cohort admin page's revoke path).
    for row in c.execute("SELECT username, cohort_countries FROM users WHERE cohort_countries IS NOT NULL"):
        for country in (row["cohort_countries"] or "").split(","):
            country = country.strip()
            if country:
                c.execute(
                    "INSERT OR IGNORE INTO cohort_country_access (username, cohort_key, country) "
                    "VALUES (?, 'precise', ?)",
                    (row["username"], country)
                )
        c.execute("UPDATE users SET cohort_countries = NULL WHERE username = ?", (row["username"],))
    c.commit()
    return c


def get_cohort_access(username: str) -> bool:
    row = get_user(username)
    return bool(row and row["cohort_access"])


def set_cohort_access(username: str, allowed: bool):
    conn = _conn()
    conn.execute("UPDATE users SET cohort_access = ? WHERE username = ?", (1 if allowed else 0, username))
    conn.commit()
    conn.close()


def get_hub_username(username: str):
    row = get_user(username)
    return row["hub_username"] if row else None


def get_cohort_countries(username: str, cohort_key: str = "precise"):
    """List of country names this account may query for the given cohort.
    Empty list = no access to any country yet for THAT cohort, even if
    cohort_access is on and/or the account has countries granted for a
    different cohort. cohort_key defaults to 'precise' only so callers
    written before DHS existed keep working unchanged; new call sites
    should always pass it explicitly."""
    conn = _conn()
    rows = conn.execute(
        "SELECT country FROM cohort_country_access WHERE username = ? AND cohort_key = ?",
        (username, cohort_key)
    ).fetchall()
    conn.close()
    return sorted(r["country"] for r in rows)


def set_cohort_countries(username: str, countries, cohort_key: str = "precise"):
    conn = _conn()
    conn.execute("DELETE FROM cohort_country_access WHERE username = ? AND cohort_key = ?",
                 (username, cohort_key))
    for country in sorted(set(countries or [])):
        conn.execute(
            "INSERT INTO cohort_country_access (username, cohort_key, country) VALUES (?, ?, ?)",
            (username, cohort_key, country)
        )
    conn.commit()
    conn.close()


def get_all_cohort_country_access(username: str):
    """{cohort_key: [countries...]} for every cohort this account has any
    country granted for — used by the admin page to render one checklist
    per cohort without N separate queries."""
    conn = _conn()
    rows = conn.execute(
        "SELECT cohort_key, country FROM cohort_country_access WHERE username = ? ORDER BY cohort_key, country",
        (username,)
    ).fetchall()
    conn.close()
    out = {}
    for r in rows:
        out.setdefault(r["cohort_key"], []).append(r["country"])
    return out


def set_hub_username(username: str, hub_username: str):
    conn = _conn()
    conn.execute("UPDATE users SET hub_username = ? WHERE username = ?", ((hub_username or None), username))
    conn.commit()
    conn.close()


def get_user(username: str):
    with _conn() as c:
        return c.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()


def list_users():
    with _conn() as c:
        return c.execute("SELECT * FROM users ORDER BY username").fetchall()


def login(username: str, password: str):
    row = get_user(username)
    if not row:
        return False, "User not found."
    stored = row['password_hash']
    if isinstance(stored, str):
        stored = stored.encode()
    if not bcrypt.checkpw(password.encode(), stored):
        return False, "Incorrect password."
    if row['is_denied']:
        return False, "This registration was declined. Contact the admin if you think this is a mistake."
    if not row['is_approved']:
        return False, "Account pending admin approval. You will be notified by email once approved."
    conn = _conn()
    conn.execute(
        "UPDATE users SET last_login = ? WHERE username = ?",
        (datetime.utcnow().isoformat(), username)
    )
    conn.commit()
    conn.close()
    return True, "OK"


def register(username: str, password: str, email: str):
    if get_user(username):
        return False, "Username already taken."
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
            (username, email, hashed)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        return False, "Username already taken."
    finally:
        conn.close()
    _notify_admin(username, email)
    return True, "Account created — awaiting admin approval."


def create_session_token(username: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(seconds=SESSION_TOKEN_TTL_SECONDS)).isoformat()
    conn = _conn()
    conn.execute(
        "INSERT INTO session_tokens (token, username, expires_at) VALUES (?, ?, ?)",
        (token, username, expires),
    )
    conn.commit()
    conn.close()
    return token


def validate_session_token(token: str):
    """Returns the username for a valid, unexpired session token, else None."""
    if not token:
        return None
    conn = _conn()
    row = conn.execute(
        "SELECT username, expires_at FROM session_tokens WHERE token = ?", (token,)
    ).fetchone()
    if not row:
        conn.close()
        return None
    if datetime.utcnow().isoformat() > row["expires_at"]:
        conn.execute("DELETE FROM session_tokens WHERE token = ?", (token,))
        conn.commit()
        conn.close()
        return None
    conn.close()
    return row["username"]


def revoke_session_token(token: str):
    if not token:
        return
    conn = _conn()
    conn.execute("DELETE FROM session_tokens WHERE token = ?", (token,))
    conn.commit()
    conn.close()


COOKIE_NAME = "palsearth_session"


def set_session_cookie(token: str):
    """
    Sets the session cookie via an injected script — Streamlit has no native
    set-cookie API. Must be called on every page after a fresh login (not just
    once), because st.page_link navigation to a different page does NOT carry
    the ?t= query param forward, so query params alone don't survive clicking
    to another page and then refreshing from there.
    """
    import streamlit.components.v1 as components
    components.html(f"""
    <script>
    document.cookie = "{COOKIE_NAME}={token}; path=/palsearth; max-age={SESSION_TOKEN_TTL_SECONDS}; SameSite=Lax";
    </script>
    """, height=0, width=0)


def clear_session_cookie():
    import streamlit.components.v1 as components
    components.html(f"""
    <script>
    document.cookie = "{COOKIE_NAME}=; path=/palsearth; max-age=0";
    </script>
    """, height=0, width=0)


def restore_session():
    """
    Call at the top of every page, before checking st.session_state['username'].
    Streamlit's session_state resets on every browser refresh (it's tied to the
    websocket connection, not the browser) — this restores login from a
    long-lived token instead, so a refresh doesn't sign you out.

    Checks the ?t= query param first (present right after login, on whichever
    page the login form itself lived on), then falls back to the session
    cookie (set on login, survives navigating to any other page — query
    params get dropped by Streamlit's own page_link navigation, cookies
    don't).
    """
    import streamlit as st
    if st.session_state.get("username"):
        return
    token = st.query_params.get("t") or st.context.cookies.get(COOKIE_NAME)
    if not token:
        return
    username = validate_session_token(token)
    if username:
        st.session_state["username"] = username


def get_user_by_email(email: str):
    with _conn() as c:
        return c.execute(
            "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
        ).fetchone()


def request_password_reset(email: str):
    """
    Always returns a generic success message regardless of whether the email
    matches an account — avoids leaking which emails are registered.
    """
    generic_msg = "If that email is registered and approved, a reset link has been sent."
    email = (email or "").strip()
    if not email:
        return True, generic_msg

    user = get_user_by_email(email)
    if user and user["is_approved"]:
        token = _create_reset_token(user["id"])
        reset_url = f"{PALSEARTH_URL}?reset_token={token}"
        _send_reset_email(user["email"], user["username"], reset_url)
    return True, generic_msg


def _create_reset_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(seconds=RESET_TOKEN_TTL_SECONDS)).isoformat()
    conn = _conn()
    conn.execute("DELETE FROM password_reset_tokens WHERE user_id = ?", (user_id,))
    conn.execute(
        "INSERT INTO password_reset_tokens (token, user_id, expires_at) VALUES (?, ?, ?)",
        (token, user_id, expires),
    )
    conn.commit()
    conn.close()
    return token


def verify_reset_token(token: str):
    """Returns the user_id for a valid, unexpired token, else None."""
    conn = _conn()
    row = conn.execute(
        "SELECT user_id, expires_at FROM password_reset_tokens WHERE token = ?", (token,)
    ).fetchone()
    if not row:
        conn.close()
        return None
    if datetime.utcnow().isoformat() > row["expires_at"]:
        conn.execute("DELETE FROM password_reset_tokens WHERE token = ?", (token,))
        conn.commit()
        conn.close()
        return None
    conn.close()
    return row["user_id"]


def reset_password(token: str, new_password: str):
    if not token or not new_password:
        return False, "Token and password are required."
    if len(new_password) < 8:
        return False, "Password must be at least 8 characters."

    user_id = verify_reset_token(token)
    if not user_id:
        return False, "This reset link is invalid or has expired — request a new one."

    hashed = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    conn = _conn()
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hashed, user_id))
    conn.execute("DELETE FROM password_reset_tokens WHERE token = ?", (token,))
    conn.commit()
    conn.close()
    return True, "Password updated — you can now sign in."


def _send_reset_email(to_email: str, username: str, reset_url: str):
    try:
        body = (
            f"Hi {username},\n\n"
            f"You requested a password reset for your PALs Earth account.\n\n"
            f"Click the link below to set a new password (valid for 1 hour):\n"
            f"{reset_url}\n\n"
            f"If you did not request this, ignore this email.\n\n"
            f"PALS Lab Team"
        )
        msg = MIMEText(body)
        msg['Subject'] = '[PALs Earth] Password reset request'
        msg['From'] = SMTP_FROM
        msg['To'] = to_email
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(SMTP_FROM, SMTP_PASS)
            s.send_message(msg)
    except Exception:
        # Never let email failure break the response, but do surface it —
        # a silent SMTP failure here once meant a week of missed admin
        # notifications going unnoticed (2026-09-30 incident).
        logger.exception("_send_reset_email failed for %s", to_email)


def _notify_admin(username: str, email: str):
    try:
        body = (
            f"New PALs Earth registration\n"
            f"  Username : {username}\n"
            f"  Email    : {email}\n"
            f"  Time     : {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"Review and approve or deny it here:\n"
            f"  {PENDING_URL}\n\n"
            f"(Or, if that's unreachable, run:\n"
            f"  sqlite3 {DB_PATH} "
            f"\"UPDATE users SET is_approved=1 WHERE username='{username}';\")\n"
        )
        msg = MIMEText(body)
        msg['Subject'] = f'[PALs Earth] New registration: {username}'
        msg['From'] = SMTP_FROM
        msg['To'] = ADMIN_EMAIL
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(SMTP_FROM, SMTP_PASS)
            s.send_message(msg)
    except Exception:
        logger.exception("_notify_admin failed for signup %s <%s>", username, email)
