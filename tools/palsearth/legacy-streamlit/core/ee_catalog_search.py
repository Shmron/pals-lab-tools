"""
Searches the real Earth Engine dataset catalog (official + sat-io community)
so Shmron AI can look up a plausible collection id instead of guessing one
from its own training knowledge and hoping — which is exactly how the GHAP
PM2.5 dataset got mis-identified four times in a row before a human supplied
the correct asset id (see memory: the asset is `GHAP_D1K_PM25`, Shmron kept
guessing variants with `PM25`/`D1K` transposed).

Two flat, pre-aggregated JSON indexes (maintained by samapriya, used by many
community GEE tools) cover this without needing to crawl Earth Engine's own
deeply-nested STAC catalog (one JSON file per provider namespace, hundreds of
them):
  - official: ~900 entries, Google's own public catalog
  - community: ~4400 entries, the sat-io community catalog (this is where
    GHAP and the Relative Wealth Index both come from)

Results are still just candidates — nothing here is used for extraction
without going through propose_custom_dataset's real, read-only Earth Engine
verification first, same as a manually-guessed candidate would.
"""
import json
import time
import urllib.request

OFFICIAL_URL = 'https://raw.githubusercontent.com/samapriya/Earth-Engine-Datasets-List/master/gee_catalog.json'
COMMUNITY_URL = 'https://raw.githubusercontent.com/samapriya/awesome-gee-community-datasets/master/community_datasets.json'

CACHE_PATH = '/home/USER/PRECISE/palsearth/ee_catalog_search_cache.json'
CACHE_TTL_SECONDS = 24 * 3600
FETCH_TIMEOUT = 15

_mem_cache = None  # {'fetched_at': float, 'items': [...]}


def _fetch_json(url):
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _build_index():
    official = _fetch_json(OFFICIAL_URL)
    for d in official:
        d['source'] = 'official'
    community = _fetch_json(COMMUNITY_URL)
    for d in community:
        d['source'] = 'community'
    return official + community


def _load_index(force_refresh=False):
    """
    In-memory cache first, then an on-disk cache (survives process restarts
    without a network round trip), then a real fetch — each step falling
    back to the next on failure, and a stale cache beats no cache at all if
    the network is briefly unavailable.
    """
    global _mem_cache
    now = time.time()

    if not force_refresh and _mem_cache and (now - _mem_cache['fetched_at']) < CACHE_TTL_SECONDS:
        return _mem_cache['items']

    if not force_refresh:
        try:
            with open(CACHE_PATH, 'r', encoding='utf-8') as f:
                on_disk = json.load(f)
            if (now - on_disk['fetched_at']) < CACHE_TTL_SECONDS:
                _mem_cache = on_disk
                return on_disk['items']
        except (OSError, json.JSONDecodeError, KeyError):
            pass

    try:
        items = _build_index()
        _mem_cache = {'fetched_at': now, 'items': items}
        try:
            with open(CACHE_PATH, 'w', encoding='utf-8') as f:
                json.dump(_mem_cache, f)
        except OSError:
            pass
        return items
    except Exception:
        # Network down and no fresh cache — fall back to whatever we have,
        # even if stale, rather than fail the whole tool call.
        if _mem_cache:
            return _mem_cache['items']
        try:
            with open(CACHE_PATH, 'r', encoding='utf-8') as f:
                on_disk = json.load(f)
            _mem_cache = on_disk
            return on_disk['items']
        except (OSError, json.JSONDecodeError, KeyError):
            raise


def search_catalog(query, max_results=8):
    """
    Token-based scoring search across title/tags/provider/thematic_group —
    not a literal substring match, so a multi-word query like "burned area"
    correctly matches "MODIS Burned Area Monthly" even with word order or
    extra words in between. Returns the catalog's own fields plus 'source'
    ('official' or 'community'); callers still need propose_custom_dataset
    to confirm the actual band names before anything is extracted.
    """
    items = _load_index()
    tokens = [t for t in query.lower().split() if t]
    if not tokens:
        return []
    scored = []
    for d in items:
        hay = ' '.join(str(d.get(k, '')) for k in ('title', 'tags', 'provider', 'thematic_group')).lower()
        score = sum(1 for t in tokens if t in hay)
        if score:
            scored.append((score, d))
    scored.sort(key=lambda x: -x[0])
    out = []
    for _, d in scored[:max_results]:
        out.append({
            'id': d.get('id'),
            'title': d.get('title'),
            'type': d.get('type'),
            'provider': d.get('provider'),
            'tags': d.get('tags'),
            'source': d.get('source'),
        })
    return out
