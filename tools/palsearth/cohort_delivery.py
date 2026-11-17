"""
Delivers a finished Cohort Exposure Assignment output file straight into a
PALSlab Hub (JupyterHub) user's own home directory, instead of making them
download it and re-upload it into a notebook session.

The PALs Earth account running the job and the PALSlab Hub account that
receives the file are two different identities (two different account
systems) — core.auth.get_hub_username() is the one admin-set link between
them (see core/auth.py's hub_username column).

Requires /etc/palslab-hub/jupyterhub_config.py's pre_spawn_hook to have
already created ~/CohortExposures/ (owned <user>:pals, mode 2770) the last
time that hub account logged in — this app runs as user "rutendo", who is a
member of the "pals" group but not of any individual user's own primary
group, so it can only write into a folder that already exists with the
right group ownership; it can't create the folder itself.

Mode-B (new-exposure) jobs run asynchronously through the shared
core.worker/core.jobs_db pipeline with no completion callback to hook into,
so a background thread here polls pending jobs and delivers each one once
it reaches 'complete'.
"""
import os
import glob
import shutil
import sqlite3
import threading
import time
import datetime

from core import auth

LOCAL_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cohort_delivery.db')
PALSEARTH_JOBS_DIR = '/home/USER/PRECISE/palsearth/jobs'

_POLL_SECONDS = 10


def _conn():
    con = sqlite3.connect(LOCAL_DB)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = _conn()
    con.execute('''
        CREATE TABLE IF NOT EXISTS pending_delivery (
            job_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            label TEXT,
            created_at TEXT NOT NULL
        )
    ''')
    con.execute('''
        CREATE TABLE IF NOT EXISTS deliveries (
            job_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            dest_path TEXT,
            status TEXT NOT NULL,
            delivered_at TEXT NOT NULL
        )
    ''')
    con.commit()
    con.close()


init_db()


def hub_dest_dir(hub_username):
    return f"/home/{hub_username}/CohortExposures"


def deliver_file(username, src_path):
    """username is the PALs Earth account; resolves to its linked hub
    account and copies src_path (+ any _metadata sidecar) there. Raises
    RuntimeError with a user-facing message if no hub account is linked yet
    or that hub account has never logged into the hub."""
    hub_username = auth.get_hub_username(username)
    if not hub_username:
        raise RuntimeError(
            "Your PALs Earth account isn't linked to a PALSlab Hub account yet. "
            "Ask an admin to set it on the Cohort admin page."
        )
    dest_dir = hub_dest_dir(hub_username)
    if not os.path.isdir(dest_dir):
        raise RuntimeError(
            f"{dest_dir} doesn't exist yet. Log into https://pals.placealert.org/hub/ "
            "once (as that hub account), then retry."
        )
    stem = os.path.splitext(src_path)[0]
    main_dest = None
    for sibling in [src_path] + glob.glob(stem + '_metadata.*'):
        if not os.path.exists(sibling):
            continue
        out_path = os.path.join(dest_dir, os.path.basename(sibling))
        shutil.copy2(sibling, out_path)
        try:
            os.chmod(out_path, 0o660)
        except OSError:
            pass
        if sibling == src_path:
            main_dest = out_path
    return main_dest


def record_delivery(job_id, username, dest_path, status):
    con = _conn()
    con.execute(
        "INSERT OR REPLACE INTO deliveries (job_id, username, dest_path, status, delivered_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (job_id, username, dest_path, status, datetime.datetime.utcnow().isoformat())
    )
    con.execute("DELETE FROM pending_delivery WHERE job_id=?", (job_id,))
    con.commit()
    con.close()


def queue_delivery(job_id, username, label):
    con = _conn()
    con.execute(
        "INSERT OR REPLACE INTO pending_delivery (job_id, username, label, created_at) VALUES (?, ?, ?, ?)",
        (job_id, username, label, datetime.datetime.utcnow().isoformat())
    )
    con.commit()
    con.close()


def get_delivery(job_id):
    con = _conn()
    row = con.execute("SELECT * FROM deliveries WHERE job_id=?", (job_id,)).fetchone()
    con.close()
    return dict(row) if row else None


def _pending_jobs():
    con = _conn()
    rows = con.execute("SELECT * FROM pending_delivery").fetchall()
    con.close()
    return [dict(r) for r in rows]


def _poll_loop():
    from core.jobs_db import get_job

    while True:
        try:
            for pending in _pending_jobs():
                job = get_job(pending['job_id'])
                if not job:
                    record_delivery(pending['job_id'], pending['username'], None, 'job_not_found')
                    continue
                if job['status'] == 'complete' and job.get('output_filename'):
                    src = os.path.join(PALSEARTH_JOBS_DIR, job['output_filename'])
                    try:
                        dest = deliver_file(pending['username'], src)
                        record_delivery(pending['job_id'], pending['username'], dest, 'delivered')
                    except Exception as e:
                        record_delivery(pending['job_id'], pending['username'], None, f'delivery_failed: {e}')
                elif job['status'] == 'failed':
                    record_delivery(pending['job_id'], pending['username'], None, 'extraction_failed')
        except Exception as e:
            print(f"[cohort_delivery] poll loop error: {e}")
        time.sleep(_POLL_SECONDS)


_watcher_started = False


def start_watcher():
    global _watcher_started
    if _watcher_started:
        return
    _watcher_started = True
    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()
