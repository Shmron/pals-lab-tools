#!/usr/bin/env python3
"""
linkedin_sync.py — Pull recent posts from the Place Alert Labs LinkedIn
company page and write them into /var/www/website/data/news.json, which the
website's news feed (assets/main.js) reads live on every page load.

Design goals:
  * SAFE BY DEFAULT — if credentials are missing or the API errors, the
    existing news.json is left completely untouched, so the site never breaks.
  * NO 3rd-PARTY DEPS — uses only the Python standard library.
  * Optional automatic token refresh if a refresh token + client creds are set.

Configuration is read from data/.linkedin.env (KEY=VALUE lines). See
scripts/LINKEDIN_SETUP.md for how to obtain each value.

Required:
  LINKEDIN_ACCESS_TOKEN   OAuth2 access token with r_organization_social
  LINKEDIN_ORG_ID         Numeric organization id (e.g. 12345678)

Optional (enables auto-refresh of the access token when it expires):
  LINKEDIN_CLIENT_ID
  LINKEDIN_CLIENT_SECRET
  LINKEDIN_REFRESH_TOKEN
  LINKEDIN_API_VERSION    LinkedIn-Version header (default: 202506)
  MAX_ITEMS               how many posts to publish (default: 6)
"""

import json
import os
import sys
import time
import datetime
import urllib.request
import urllib.parse
import urllib.error

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(BASE_DIR, "data", ".linkedin.env")
NEWS_PATH = os.path.join(BASE_DIR, "data", "news.json")
DEFAULT_URL = "https://www.linkedin.com/company/place-alert-labs/posts/?feedView=all"


def log(msg):
    print(f"[linkedin_sync {datetime.datetime.now().isoformat(timespec='seconds')}] {msg}")


def load_env(path):
    env = {}
    if not os.path.exists(path):
        return env
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def save_env(path, env):
    """Persist env back (used after refreshing the access token)."""
    lines = ["# Managed by linkedin_sync.py — edit values, keep KEY=VALUE format", ""]
    for k in sorted(env):
        lines.append(f"{k}={env[k]}")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def http_json(url, headers=None, data=None, method="GET"):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def refresh_access_token(env):
    """Exchange a refresh token for a new access token, if configured."""
    if not all(env.get(k) for k in ("LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET", "LINKEDIN_REFRESH_TOKEN")):
        return False
    log("Access token rejected; attempting refresh…")
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": env["LINKEDIN_REFRESH_TOKEN"],
        "client_id": env["LINKEDIN_CLIENT_ID"],
        "client_secret": env["LINKEDIN_CLIENT_SECRET"],
    }).encode()
    try:
        tok = http_json(
            "https://www.linkedin.com/oauth/v2/accessToken",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=body, method="POST",
        )
    except urllib.error.URLError as e:
        log(f"Refresh failed: {e}")
        return False
    if "access_token" not in tok:
        log("Refresh response had no access_token")
        return False
    env["LINKEDIN_ACCESS_TOKEN"] = tok["access_token"]
    if tok.get("refresh_token"):
        env["LINKEDIN_REFRESH_TOKEN"] = tok["refresh_token"]
    save_env(ENV_PATH, env)
    log("Access token refreshed and saved.")
    return True


def fetch_posts(env):
    org_urn = f"urn:li:organization:{env['LINKEDIN_ORG_ID']}"
    version = env.get("LINKEDIN_API_VERSION", "202506")
    params = urllib.parse.urlencode({
        "author": org_urn,
        "q": "author",
        "count": str(int(env.get("MAX_ITEMS", "6")) + 4),  # over-fetch, filter reshares
        "sortBy": "LAST_MODIFIED",
    })
    url = f"https://api.linkedin.com/rest/posts?{params}"
    headers = {
        "Authorization": f"Bearer {env['LINKEDIN_ACCESS_TOKEN']}",
        "LinkedIn-Version": version,
        "X-Restli-Protocol-Version": "2.0.0",
    }
    return http_json(url, headers=headers)


def post_permalink(urn):
    # urn like urn:li:share:123 or urn:li:ugcPost:123
    return f"https://www.linkedin.com/feed/update/{urllib.parse.quote(urn, safe=':')}"


def to_items(api_response, max_items):
    """Map LinkedIn /rest/posts response into the news.json item schema."""
    items = []
    for p in api_response.get("elements", []):
        text = ((p.get("commentary") or "").strip())
        if not text:
            continue  # skip empty / pure-media posts without caption
        # Title = first line / sentence; description = the rest (trimmed).
        first_break = min([i for i in (text.find("\n"), text.find(". ")) if i != -1] or [len(text)])
        title = text[:first_break].strip().rstrip(".")
        if len(title) > 120:
            title = title[:117].rstrip() + "…"
        description = text.strip()
        if len(description) > 320:
            description = description[:317].rstrip() + "…"
        urn = p.get("id") or ""
        created = p.get("createdAt") or p.get("publishedAt")
        date_label = ""
        published_iso = None
        if created:
            dt = datetime.datetime.fromtimestamp(created / 1000, tz=datetime.timezone.utc)
            date_label = dt.strftime("%B %Y")
            published_iso = dt.isoformat()
        items.append({
            "id": urn or f"li-{int(time.time())}-{len(items)}",
            "title": title or "Place Alert Labs update",
            "description": description,
            "category": "UPDATE",
            "date": date_label,
            "published_at": published_iso,
            "url": post_permalink(urn) if urn else DEFAULT_URL,
            "tags": [],
        })
        if len(items) >= max_items:
            break
    return items


def write_news(items):
    payload = {
        "version": 1,
        "source": "linkedin",
        "updated_at": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
        "items": items,
    }
    tmp = NEWS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, NEWS_PATH)  # atomic swap — readers never see a half-written file


def main():
    env = load_env(ENV_PATH)
    if not env.get("LINKEDIN_ACCESS_TOKEN") or not env.get("LINKEDIN_ORG_ID"):
        log("No credentials in data/.linkedin.env — leaving news.json unchanged (no-op).")
        return 0

    max_items = int(env.get("MAX_ITEMS", "6"))
    try:
        resp = fetch_posts(env)
    except urllib.error.HTTPError as e:
        if e.code == 401 and refresh_access_token(env):
            try:
                resp = fetch_posts(env)
            except urllib.error.HTTPError as e2:
                log(f"API error after refresh: {e2.code} {e2.reason} — keeping existing news.json.")
                return 1
        else:
            log(f"API error: {e.code} {e.reason} — keeping existing news.json.")
            return 1
    except urllib.error.URLError as e:
        log(f"Network error: {e} — keeping existing news.json.")
        return 1

    items = to_items(resp, max_items)
    if not items:
        log("API returned no usable posts — keeping existing news.json.")
        return 1

    write_news(items)
    log(f"Wrote {len(items)} post(s) to news.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
