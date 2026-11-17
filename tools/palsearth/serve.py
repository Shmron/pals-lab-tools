"""
Entry point for the service. Prefers waitress (a real WSGI server) and falls
back to Werkzeug's threaded server if waitress isn't installed, so the unit
file never has to change if we add it later.
"""
import os

from app import application

HOST = os.environ.get("PALSEARTH_HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", 8504))

def _resume_interrupted_jobs():
    """A restart kills in-flight extraction workers; pick their jobs back up."""
    try:
        from core.worker import recover_interrupted_jobs
        n = recover_interrupted_jobs()
        if n:
            print(f"[palsearth] resumed {n} interrupted job(s)", flush=True)
    except Exception as e:
        print(f"[palsearth] job recovery failed: {e}", flush=True)


if __name__ == "__main__":
    if os.environ.get("PALSEARTH_RECOVER_JOBS", "1") != "0":
        import threading
        threading.Timer(3, _resume_interrupted_jobs).start()
    try:
        from waitress import serve
    except ImportError:
        print(f"[palsearth] waitress not installed — using Werkzeug (threaded) "
              f"on {HOST}:{PORT}", flush=True)
        application.run(host=HOST, port=PORT, threaded=True, debug=False)
    else:
        print(f"[palsearth] waitress on {HOST}:{PORT}", flush=True)
        serve(application, host=HOST, port=PORT, threads=8,
              channel_timeout=300, ident="palsearth")
