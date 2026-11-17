#!/bin/bash
# Puts the Streamlit PALs Earth back at /palsearth/.
set -euo pipefail
CONF=/etc/nginx/conf.d/placealert.conf
UNIT=/etc/systemd/system/palsearth-flask.service
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
NB=$(ls -t $CONF.bak.cutover.* | tail -1)      # the OLDEST cutover backup = pre-cutover state
UB=$(ls -t $UNIT.bak.cutover.* | tail -1)
cp "$NB" "$CONF"; cp "$UB" "$UNIT"
systemctl daemon-reload
systemctl enable --now palsearth
systemctl restart palsearth-flask
nginx -t && systemctl reload nginx
echo "Rolled back: Streamlit at /palsearth/, Flask at /palsearth-flask/ again."
