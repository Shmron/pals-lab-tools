"""
One place that answers "what is the Anthropic API key?" for both PALs Earth
front ends (Flask and the older Streamlit app).

The key is deliberately NOT copied into this project. The catalogue's Shmron
already runs on it, held in the precise-api systemd unit, so we read it from
there: rotate it once and every app that uses this helper follows on its next
refresh (results are cached for a minute, no restart needed).

Lookup order:
  1. ANTHROPIC_API_KEY in this process's environment, if it is a real key
     (ignores empty values and the 'REPLACE_WITH_NEW_KEY' placeholder)
  2. the PALSEARTH .env files, if they define it
  3. the precise-api unit files (main file + drop-ins), read directly — they
     are world-readable, so this needs no root and no subprocess
  4. `systemctl show precise-api`, as a last resort

If a refresh finds nothing, the last key that worked is kept, so a transient
hiccup can never flip the site to "not configured".

The value is never logged or printed.
"""
import glob
import os
import re
import shlex
import subprocess
import time

_SOURCES_UNIT = "precise-api"
_ENV_FILES = (
    "/home/USER/PRECISE/palsearth_flask/.env",
    "/home/USER/PRECISE/palsearth/.env",
)
_UNIT_PATHS = ("/etc/systemd/system/precise-api.service",
               "/etc/systemd/system/precise-api.service.d")
_TTL = 60
_cache = {"at": 0.0, "key": ""}
_last_good = {"key": ""}
_KEY_RE = re.compile(r"ANTHROPIC_API_KEY=([^\s\"']+)")


def _looks_real(value):
    v = (value or "").strip()
    return v.startswith("sk-ant-") and "REPLACE" not in v.upper()


def _from_env_files():
    for path in _ENV_FILES:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("ANTHROPIC_API_KEY="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if _looks_real(val):
                            return val
        except OSError:
            continue
    return ""


def _from_unit_files():
    """Later drop-ins override earlier files, matching systemd's own rule."""
    files = [_UNIT_PATHS[0]] + sorted(glob.glob(os.path.join(_UNIT_PATHS[1], "*.conf")))
    found = ""
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                for m in _KEY_RE.finditer(f.read()):
                    if _looks_real(m.group(1)):
                        found = m.group(1)
        except OSError:
            continue
    return found


def _from_service(unit=_SOURCES_UNIT):
    try:
        out = subprocess.run(
            ["systemctl", "show", unit, "-p", "Environment", "--value"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return ""
    try:
        parts = shlex.split(out)
    except ValueError:
        parts = out.split()
    for part in parts:
        if part.startswith("ANTHROPIC_API_KEY="):
            val = part.split("=", 1)[1]
            if _looks_real(val):
                return val
    return ""


def get_anthropic_key():
    """Returns the key, or '' if none can be found. Cached for a minute."""
    now = time.time()
    if _cache["key"] and now - _cache["at"] < _TTL:
        return _cache["key"]

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not _looks_real(key):
        key = _from_env_files() or _from_unit_files() or _from_service()
    if not _looks_real(key):
        key = _last_good["key"]          # keep working through a failed refresh
    if _looks_real(key):
        _last_good["key"] = key
        _cache.update(at=now, key=key)
        return key
    return ""
