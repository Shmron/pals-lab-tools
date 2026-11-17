#!/bin/bash
# Replaces the Streamlit PALs Earth with the Flask one at /palsearth/.
# Run:  sudo bash /home/USER/PRECISE/palsearth_flask/cutover.sh
# Undo: sudo bash /home/USER/PRECISE/palsearth_flask/rollback.sh
set -euo pipefail
APP=/home/USER/PRECISE/palsearth_flask
CONF=/etc/nginx/conf.d/placealert.conf
UNIT=/etc/systemd/system/palsearth-flask.service
STAMP=$(date +%Y%m%d%H%M%S)
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }

echo "── 1. Flask unit: serve at /palsearth (drop the temporary prefix)"
cp "$UNIT" "$UNIT.bak.cutover.$STAMP"
install -m 644 "$APP/palsearth-flask.service.cutover" "$UNIT"
systemctl daemon-reload

echo "── 2. nginx: /palsearth/ -> Flask"
cp "$CONF" "$CONF.bak.cutover.$STAMP"
python3 "$APP/nginx_cutover.py" "$CONF"
if ! nginx -t; then
  echo "nginx -t FAILED - restoring backup"; cp "$CONF.bak.cutover.$STAMP" "$CONF"; exit 1
fi

echo "── 3. restart Flask on the new prefix, then reload nginx"
systemctl restart palsearth-flask
sleep 4
systemctl is-active --quiet palsearth-flask || { echo "Flask failed to start"; journalctl -u palsearth-flask -n 15 --no-pager; exit 1; }
systemctl reload nginx

echo "── 4. smoke test through nginx (retries: a reload takes a moment to apply)"
ok=0
for i in 1 2 3 4 5 6 7 8 9 10; do
  out=$(curl -sk -o /dev/null -w "%{http_code} %{redirect_url}" -H "Host: portal.placealert.org" https://127.0.0.1/palsearth/ || true)
  code=${out%% *}; loc=${out#* }
  echo "   try $i: $code $loc"
  if [ "$code" = "302" ] && [[ "$loc" == *"/palsearth/auth/signin" ]]; then ok=1; break; fi
  sleep 2
done
if [ "$ok" != "1" ]; then
  echo "   Flask never answered at /palsearth/ - rolling back"; bash "$APP/rollback.sh"; exit 1
fi

echo "── 5. retire Streamlit (kept on disk as a rollback)"
systemctl disable --now palsearth || true

echo
echo "Done. PALs Earth (Flask) is now live at https://portal.placealert.org/palsearth/"
echo "Old links (/palsearth/Extract, /My_Jobs, /Help and /palsearth-flask/...) redirect."
