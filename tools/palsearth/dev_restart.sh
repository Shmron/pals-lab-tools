#!/bin/bash
# Dev-only launcher: stops any previous instance by pidfile, starts a new one.
cd /home/USER/PRECISE/palsearth_flask
PIDFILE=/tmp/pef.pid
if [ -f "$PIDFILE" ]; then
  kill "$(cat $PIDFILE)" 2>/dev/null
  sleep 1
fi
setsid nohup env \
  PALSEARTH_ROOT=/home/USER/PRECISE/palsearth \
  PORT="${PORT:-8504}" \
  PALSEARTH_HOST="${PALSEARTH_HOST:-127.0.0.1}" \
  PALSEARTH_COOKIE_SECURE="${PALSEARTH_COOKIE_SECURE:-1}" \
  PALSEARTH_SECRET_KEY="${PALSEARTH_SECRET_KEY:-devtest}" \
  /opt/anaconda3/bin/python ./serve.py > /tmp/pef.log 2>&1 < /dev/null &
echo $! > "$PIDFILE"
sleep 4
echo "pid $(cat $PIDFILE):"
cat /tmp/pef.log
