"""
Server-side per-user state for the Flask app, replacing st.session_state.

Streamlit kept staged uploads, custom datasets, the pending plan and the AI
message history in a per-websocket dict. Flask has no equivalent, and a cookie
is far too small for an Anthropic message history, so it lives in SQLite keyed
by username. That also means a service restart (or a deploy) no longer throws
away someone's half-finished conversation, which the Streamlit version did.
"""
import json
import os
import sqlite3
import threading

DB_PATH = os.environ.get(
    "PALSEARTH_STATE_DB", "/home/USER/PRECISE/palsearth/palsearth_state.db"
)
_lock = threading.Lock()

_KEYS_PERSISTED = (
    "staged_upload", "custom_datasets", "pending_plan",
    "last_ai_job_id", "ai_messages", "transcript",
    "manual_selection",   # "Add to extraction" from the Datasets page
    "_flash",             # one-shot messages, shown once then cleared
    "cohort_pending_b",   # staged Cohort Mode-B plan awaiting stat-feasibility confirmation
)


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.execute("""
        CREATE TABLE IF NOT EXISTS ai_state (
            username   TEXT PRIMARY KEY,
            data       TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    c.commit()
    return c


def _default(obj):
    """Anthropic SDK content blocks are pydantic models, not plain dicts."""
    for attr in ("model_dump", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    return str(obj)


def load(username):
    with _lock:
        c = _conn()
        row = c.execute("SELECT data FROM ai_state WHERE username=?", (username,)).fetchone()
        c.close()
    if not row:
        return {}
    try:
        data = json.loads(row[0])
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save(username, state):
    keep = {k: v for k, v in state.items() if k in _KEYS_PERSISTED}
    blob = json.dumps(keep, default=_default)
    with _lock:
        c = _conn()
        c.execute(
            "INSERT INTO ai_state (username, data, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(username) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (username, blob),
        )
        c.commit()
        c.close()


def clear_chat(username):
    """Drops the conversation but keeps the staged upload and custom datasets."""
    state = load(username)
    for k in ("ai_messages", "transcript", "pending_plan"):
        state.pop(k, None)
    save(username, state)
    return state
