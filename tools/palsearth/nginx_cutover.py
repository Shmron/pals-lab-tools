"""Swap the nginx /palsearth/ block from Streamlit (8503) to Flask (8504) and
turn the temporary /palsearth-flask/ block into a redirect. Idempotent."""
import sys

path = sys.argv[1]
s = open(path).read()

NEW_MAIN = """    location /palsearth/ {
        proxy_pass http://127.0.0.1:8504/palsearth/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300;
        client_max_body_size 220M;
    }
"""
NEW_ALIAS = """    location /palsearth-flask/ {
        rewrite ^/palsearth-flask/(.*)$ /palsearth/$1 permanent;
    }
"""

def span(name):
    head = f"    location {name} {{"
    i = s.index(head)
    j = s.index("\n    }\n", i) + len("\n    }\n")
    return i, j

i, j = span("/palsearth/")
if "127.0.0.1:8504/palsearth/" in s[i:j]:
    print("main block already points at Flask")
else:
    s = s[:i] + NEW_MAIN + s[j:]
    print("main /palsearth/ block -> Flask :8504")

if "location /palsearth-flask/ {" in s:
    i, j = span("/palsearth-flask/")
    if "rewrite" in s[i:j]:
        print("alias block already a redirect")
    else:
        s = s[:i] + NEW_ALIAS + s[j:]
        print("/palsearth-flask/ -> permanent redirect to /palsearth/")
open(path, "w").write(s)
