import os
import sys
import traceback
from multiprocessing import Process

# Ensure the palsearth package root is on sys.path when running in subprocess
PALSEARTH_ROOT = '/home/USER/PRECISE/palsearth'


def _run_job(job_id):
    """Target function for subprocess: runs extraction for a given job_id."""
    if PALSEARTH_ROOT not in sys.path:
        sys.path.insert(0, PALSEARTH_ROOT)

    from core.jobs_db import get_job, update_job
    from core.extractor import run_extraction
    from core.notifications import notify_complete, notify_failed
    from core.auth import get_user

    job = get_job(job_id)
    if not job:
        return

    _write_pid(job_id)
    update_job(job_id, status='running', progress=0)

    def update_progress(pct, status=None, error=None):
        kwargs = {'progress': pct}
        if status:
            kwargs['status'] = status
        if error:
            kwargs['error_msg'] = error
        update_job(job_id, **kwargs)

    try:
        output_path = run_extraction(job, update_progress_fn=update_progress)
        output_filename = os.path.basename(output_path)
        update_job(job_id, status='complete', progress=100,
                   output_filename=output_filename)
        # Notify user
        user_row = get_user(job['username'])
        if user_row and user_row[3]:
            job_updated = get_job(job_id)
            notify_complete(job_updated, user_row[3])
            update_job(job_id, notified=1)
    except Exception as e:
        err_msg = traceback.format_exc()
        print(f"[worker] Job {job_id} failed: {err_msg}")
        update_job(job_id, status='failed', error_msg=str(e)[:2000])
        # Notify user of failure
        try:
            from core.auth import get_user
            user_row = get_user(job['username'])
            if user_row and user_row[3]:
                job_updated = get_job(job_id)
                notify_failed(job_updated, user_row[3])
                update_job(job_id, notified=1)
        except Exception:
            pass
    finally:
        try:
            os.remove(_pid_path(job_id))
        except OSError:
            pass


def _pid_path(job_id):
    return os.path.join(PALSEARTH_ROOT, 'jobs', f'{job_id}.pid')


def _write_pid(job_id):
    try:
        with open(_pid_path(job_id), 'w') as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def _worker_alive(job_id):
    """True only if the recorded pid is still a running python process."""
    try:
        pid = int(open(_pid_path(job_id)).read().strip())
        os.kill(pid, 0)
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            return b'python' in f.read()
    except (OSError, ValueError):
        return False


def recover_interrupted_jobs():
    """
    Restart jobs that are marked queued/running but whose worker no longer
    exists (a deploy, restart or crash killed it). Returns how many restarted.
    """
    if PALSEARTH_ROOT not in sys.path:
        sys.path.insert(0, PALSEARTH_ROOT)
    from core.jobs_db import list_all_jobs, update_job

    restarted = 0
    for job in list_all_jobs():
        if job['status'] in ('queued', 'running') and not _worker_alive(job['id']):
            update_job(job['id'], status='queued', progress=0, error_msg=None)
            run_job_in_background(job['id'])
            restarted += 1
            print(f"[worker] resumed interrupted job {job['id']}", flush=True)
    return restarted


def run_job_in_background(job_id):
    """Launch extraction job in a daemon subprocess."""
    p = Process(target=_run_job, args=(job_id,))
    p.daemon = True
    p.start()
    return p


def cancel_job(job_id):
    """
    Kill a running job's worker subprocess via its recorded PID (the same
    file _write_pid wrote at job start). SIGTERM is not caught inside the
    worker, so its own `finally` cleanup never runs — this removes the pid
    file itself rather than relying on that. Safe to call on a job with no
    live worker (e.g. already finished): just a no-op pid-file cleanup.
    Returns True if a live process was actually signalled.
    """
    import signal
    killed = False
    try:
        with open(_pid_path(job_id)) as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGTERM)
        killed = True
    except (OSError, ValueError):
        pass
    try:
        os.remove(_pid_path(job_id))
    except OSError:
        pass
    return killed
