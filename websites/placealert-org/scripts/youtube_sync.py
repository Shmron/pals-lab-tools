#!/usr/bin/env python3
"""
youtube_sync.py — Read the Place Alert Labs YouTube channel's public RSS feed
and write the latest videos to /var/www/website/data/videos.json, which the
Media page reads to populate its two auto-updating video players.

Public RSS — no API key needed. Safe by default: on any error the existing
videos.json is left untouched so the page never breaks.
"""

import json
import os
import sys
import datetime
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

CHANNEL_ID = "UCyCK2Fnvwmm3hHlC-vkiBnw"  # @PlaceAlertLabs
FEED_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
MAX_ITEMS = 6  # store a few; the page uses the top 2

# Videos to never show on the Media page, even if they're among the channel's
# latest uploads (e.g. content not meant for the public site).
EXCLUDE_IDS = {
    "8iTirQ7pcAg",  # Road Mothers Application (RoadMApp) — not a PALs media item
}

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(BASE_DIR, "data", "videos.json")

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


def log(msg):
    print(f"[youtube_sync {datetime.datetime.now().isoformat(timespec='seconds')}] {msg}")


def fetch_feed():
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": "Mozilla/5.0 (PALS website sync)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def parse(xml_bytes):
    root = ET.fromstring(xml_bytes)
    items = []
    for e in root.findall("a:entry", NS):
        vid = e.find("yt:videoId", NS)
        title = e.find("a:title", NS)
        pub = e.find("a:published", NS)
        if vid is None or not vid.text:
            continue
        if vid.text.strip() in EXCLUDE_IDS:
            continue
        items.append({
            "id": vid.text.strip(),
            "title": (title.text or "").strip() if title is not None else "",
            "published_at": (pub.text or "").strip() if pub is not None else "",
        })
        if len(items) >= MAX_ITEMS:
            break
    return items


def write(items):
    payload = {
        "channel": "@PlaceAlertLabs",
        "channel_id": CHANNEL_ID,
        "updated_at": datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
        "items": items,
    }
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, OUT_PATH)  # atomic swap


def main():
    try:
        xml_bytes = fetch_feed()
        items = parse(xml_bytes)
    except (urllib.error.URLError, ET.ParseError) as e:
        log(f"Fetch/parse failed: {e} — keeping existing videos.json.")
        return 1
    if not items:
        log("Feed had no videos — keeping existing videos.json.")
        return 1
    write(items)
    log(f"Wrote {len(items)} video(s); latest: {items[0]['id']} ({items[0]['title'][:50]}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
