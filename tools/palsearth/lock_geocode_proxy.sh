#!/bin/bash
# Restricts the geocoding proxy to this machine and its Docker networks.
# It was open to the whole internet and is being used as a free relay.
# Run: sudo bash /home/USER/PRECISE/palsearth_flask/lock_geocode_proxy.sh
set -euo pipefail
CONF=/etc/nginx/conf.d/geocoding-proxy.conf
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
if grep -q "deny all" "$CONF"; then echo "Already restricted - nothing to do."; exit 0; fi
cp "$CONF" "$CONF.bak.$(date +%Y%m%d%H%M%S)"
python3 - "$CONF" <<PY
import sys
p = sys.argv[1]
s = open(p).read()
rule = "    location / {\n        allow 127.0.0.1;\n        allow 172.16.0.0/12;   # docker0 + br-* bridges (172.17-172.21)\n        deny all;\n"
n = s.count("    location / {\n")
assert n == 2, f"expected 2 location blocks, found {n}"
open(p, "w").write(s.replace("    location / {\n", rule))
print(f"restricted {n} blocks")
PY
if nginx -t; then systemctl reload nginx && echo "Done - proxy is now internal-only."
else echo "nginx -t FAILED - restoring backup"; cp "$(ls -t $CONF.bak.* | head -1)" "$CONF"; exit 1; fi
