import sqlite3
import uuid
import json
from datetime import datetime

JOBS_DB = '/home/USER/PRECISE/palsearth/palsearth_jobs.db'


def _get_conn():
    conn = sqlite3.connect(JOBS_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            datasets TEXT NOT NULL,
            start_date TEXT,
            end_date TEXT,
            geometry_type TEXT NOT NULL,
            input_filename TEXT NOT NULL,
            output_filename TEXT,
            progress INTEGER DEFAULT 0,
            error_msg TEXT,
            ee_project TEXT NOT NULL,
            stats_requested TEXT NOT NULL,
            output_format TEXT NOT NULL DEFAULT 'csv',
            notified INTEGER DEFAULT 0,
            downloaded INTEGER DEFAULT 0,
            custom_datasets TEXT,
            source TEXT NOT NULL DEFAULT 'manual',
            date_mode TEXT NOT NULL DEFAULT 'range',
            window_keys TEXT,
            ref_date_cols TEXT,
            custom_window TEXT,
            output_shape TEXT NOT NULL DEFAULT 'wide',
            temp_unit TEXT NOT NULL DEFAULT 'kelvin'
        )
    ''')
    conn.commit()
    # Migrate older DBs created before these columns existed.
    cur.execute("PRAGMA table_info(jobs)")
    existing_cols = {row[1] for row in cur.fetchall()}
    for col, ddl in (
        ('custom_datasets', "ALTER TABLE jobs ADD COLUMN custom_datasets TEXT"),
        ('source', "ALTER TABLE jobs ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"),
        ('date_mode', "ALTER TABLE jobs ADD COLUMN date_mode TEXT NOT NULL DEFAULT 'range'"),
        ('window_keys', "ALTER TABLE jobs ADD COLUMN window_keys TEXT"),
        ('ref_date_cols', "ALTER TABLE jobs ADD COLUMN ref_date_cols TEXT"),
        ('custom_window', "ALTER TABLE jobs ADD COLUMN custom_window TEXT"),
        ('output_shape', "ALTER TABLE jobs ADD COLUMN output_shape TEXT NOT NULL DEFAULT 'wide'"),
        ('temp_unit', "ALTER TABLE jobs ADD COLUMN temp_unit TEXT NOT NULL DEFAULT 'kelvin'"),
    ):
        if col not in existing_cols:
            cur.execute(ddl)
    conn.commit()
    conn.close()


def create_job(username, datasets, start_date, end_date, geometry_type,
               input_filename, ee_project, stats_requested, output_format='csv',
               custom_datasets=None, source='manual',
               date_mode='range', window_keys=None, ref_date_cols=None,
               custom_window=None, output_shape='wide', temp_unit='kelvin'):
    job_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO jobs (
            id, username, status, created_at, updated_at,
            datasets, start_date, end_date, geometry_type,
            input_filename, output_filename, progress, error_msg,
            ee_project, stats_requested, output_format, notified, downloaded,
            custom_datasets, source, date_mode, window_keys, ref_date_cols,
            custom_window, output_shape, temp_unit
        ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        job_id, username, now, now,
        json.dumps(datasets), start_date, end_date, geometry_type,
        input_filename, ee_project, json.dumps(stats_requested), output_format,
        json.dumps(custom_datasets or {}), source,
        date_mode, json.dumps(window_keys or []), json.dumps(ref_date_cols or {}),
        json.dumps(custom_window or {}), output_shape, temp_unit,
    ))
    conn.commit()
    conn.close()
    return job_id


def get_job(job_id):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def get_user_jobs(username):
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM jobs WHERE username=? ORDER BY created_at DESC", (username,))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_all_jobs():
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM jobs ORDER BY created_at DESC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


_ALLOWED_JOB_COLS = frozenset({
    'status', 'progress', 'error_msg', 'output_filename',
    'notified', 'downloaded', 'updated_at',
})

def update_job(job_id, **kwargs):
    if not kwargs:
        return
    kwargs['updated_at'] = datetime.utcnow().isoformat()
    bad = set(kwargs) - _ALLOWED_JOB_COLS
    if bad:
        raise ValueError(f"update_job: disallowed column(s): {bad}")
    set_clause = ', '.join(f"{k}=?" for k in kwargs)
    values = list(kwargs.values()) + [job_id]
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(f"UPDATE jobs SET {set_clause} WHERE id=?", values)
    conn.commit()
    conn.close()


def delete_job(job_id):
    """Removes a job row. The caller is responsible for its output files."""
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    conn.commit()
    conn.close()


# Initialise DB on import
init_db()
