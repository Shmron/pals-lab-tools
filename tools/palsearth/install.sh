#!/bin/bash
# Installs PALs Earth (Flask) alongside the live Streamlit app.
#   - Streamlit stays untouched on 8503 at /palsearth/      (your rollback)
#   - Flask goes up on 8504 at /palsearth-flask/            (what you test)
# Run with: sudo bash /home/USER/PRECISE/palsearth_flask/install.sh
set -euo pipefail

APP=/home/USER/PRECISE/palsearth_flask
CONF=/etc/nginx/conf.d/placealert.conf
STAMP=$(date +%Y%m%d%H%M%S)

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this with sudo." >&2
  exit 1
fi

echo "── 1. secret key ───────────────────────────────────────"
if [ ! -f "$APP/.env" ]; then
  {
    echo "# PALs Earth (Flask) environment."
    echo "# Paste your new Anthropic key here to switch Shmron AI back on:"
    echo "# ANTHROPIC_API_KEY=sk-ant-..."
    echo "PALSEARTH_SECRET_KEY=$(head -c 32 /dev/urandom | base64 | tr -d '=+/' )"
  } > "$APP/.env"
  chown rutendo:rutendo "$APP/.env"
  chmod 600 "$APP/.env"
  echo "   wrote $APP/.env (600, owned by rutendo)"
else
  grep -q PALSEARTH_SECRET_KEY "$APP/.env" || {
    echo "PALSEARTH_SECRET_KEY=$(head -c 32 /dev/urandom | base64 | tr -d '=+/')" >> "$APP/.env"
    echo "   added a secret key to the existing .env"
  }
  echo "   $APP/.env already exists — left alone"
fi

echo "── 2. systemd service ──────────────────────────────────"
install -m 644 "$APP/palsearth-flask.service" /etc/systemd/system/palsearth-flask.service
systemctl daemon-reload
systemctl enable palsearth-flask >/dev/null
# stop any hand-started copy from testing
[ -f /tmp/pef.pid ] && kill "$(cat /tmp/pef.pid)" 2>/dev/null || true
systemctl restart palsearth-flask
sleep 3
systemctl --no-pager --lines=8 status palsearth-flask || true

echo "── 3. nginx location block ─────────────────────────────"
if grep -q "location /palsearth-flask/" "$CONF"; then
  echo "   block already present — skipping"
else
  cp "$CONF" "$CONF.bak.flask.$STAMP"
  echo "   backed up to $CONF.bak.flask.$STAMP"
  python3 - "$CONF" <<'PY'
import sys
path = sys.argv[1]
src = open(path).read()
anchor = "    location /palsearth/ {"
assert anchor in src, "could not find the existing /palsearth/ block"
block = """    location /palsearth-flask/ {
        proxy_pass http://127.0.0.1:8504/palsearth-flask/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300;
        client_max_body_size 220M;
    }

"""
src = src.replace(anchor, block + anchor, 1)
open(path, "w").write(src)
print("   inserted /palsearth-flask/ before /palsearth/")
PY
fi

echo "── 4. test + reload nginx ──────────────────────────────"
nginx -t
systemctl reload nginx
echo
echo "════════════════════════════════════════════════════════"
echo " Done. Test the new app at:"
echo "   https://portal.placealert.org/palsearth-flask/"
echo " The old Streamlit app is untouched at:"
echo "   https://portal.placealert.org/palsearth/"
echo "════════════════════════════════════════════════════════"
