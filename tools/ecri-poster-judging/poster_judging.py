"""ECRI 2026 poster judging: device-friendly scoring for a small panel of adjudicators.

Self-contained Flask blueprint, mounted in ECRI at /ecri/judging (see app.py there).

No accounts and no email. The panel is a set of numbered seats (Adjudicator 1, 2, 3...).
On the day each adjudicator opens the site, taps their number, and sees their own posters.
The device remembers the number. The chief adjudicator signs in with a password.

Flow
  1. Round 1: every poster is scored /100 by N adjudicators (N is set by the chief).
  2. Once EVERY poster has N submitted scores, the top 5 by mean score advance
     (everyone tied at the 5th mean advances too).
  3. Final vote: each adjudicator recommends one winner with a reason. Most recommendations
     wins. A tie triggers another vote between the tied posters only, repeated until one
     wins. The round is counted automatically as soon as the last adjudicator has voted.

The chief is stored as one hidden person (is_admin = 1) who never scores or votes.
"""
import os
import re
import hmac
import random
import secrets
import sqlite3
import datetime

from flask import (Blueprint, g, request, jsonify, render_template, redirect, make_response,
                   url_for, abort, session, current_app)

bp = Blueprint('posters', __name__, template_folder='templates')

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('POSTER_DB', os.path.join(HERE, 'posters.db'))
PUBLIC_BASE = os.environ.get('POSTER_BASE_URL', 'https://portal.placealert.org/ecri/judging').rstrip('/')
CHIEF_NAME = 'Chief adjudicator'
DEFAULT_CHIEF_PASSWORD = os.environ.get('POSTER_CHIEF_PASSWORD') or secrets.token_urlsafe(10)  # set POSTER_CHIEF_PASSWORD; a random one is used if unset
DEFAULT_PANEL = 7
MAX_PANEL = 30


# key, label, max marks, prompt shown under the label
CRITERIA = [
    ('meth', 'Methodology', 30, 'Is the approach sound and appropriate?'),
    ('evid', 'Results & evidence', 25, 'Do the data support the conclusions?'),
    ('nov', 'Novelty & contribution', 20, 'What is new, and what does it add?'),
    ('rel', 'Relevance & impact', 15, 'Does it matter for the symposium and for implementation?'),
    ('clar', 'Clarity & completeness', 10, 'Is it clear, complete and well presented?'),
]
CRIT_KEYS = [c[0] for c in CRITERIA]
CRIT_MAX = {c[0]: c[2] for c in CRITERIA}
DEFAULT_PER_ABSTRACT = 2
DEFAULT_FINALISTS = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS judge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token TEXT UNIQUE NOT NULL,
    is_chair INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS poster (
    code TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    presenter TEXT NOT NULL DEFAULT '',
    theme TEXT NOT NULL DEFAULT '',
    finalist INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS assignment (
    poster TEXT NOT NULL,
    judge INTEGER NOT NULL,
    conflict INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (poster, judge)
);
CREATE TABLE IF NOT EXISTS score (
    poster TEXT NOT NULL,
    judge INTEGER NOT NULL,
    meth INTEGER, evid INTEGER, nov INTEGER, rel INTEGER, clar INTEGER,
    note TEXT NOT NULL DEFAULT '',
    submitted_at TEXT,
    PRIMARY KEY (poster, judge)
);
CREATE TABLE IF NOT EXISTS message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    poster TEXT NOT NULL,
    judge INTEGER NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vote (
    judge INTEGER NOT NULL,
    round INTEGER NOT NULL,
    poster TEXT NOT NULL,
    reason TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (judge, round)
);
CREATE TABLE IF NOT EXISTS vote_round (
    n INTEGER PRIMARY KEY,
    candidates TEXT NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS setting (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# ---------- plumbing ----------

def db():
    if 'pj_db' not in g:
        g.pj_db = sqlite3.connect(DB_PATH)
        g.pj_db.row_factory = sqlite3.Row
        g.pj_db.executescript(SCHEMA)
        for table, col, ddl in (("judge", "is_admin", "INTEGER NOT NULL DEFAULT 0"),
                                ("judge", "email", "TEXT NOT NULL DEFAULT ''"),
                                ("judge", "nonce", "TEXT NOT NULL DEFAULT ''"),
                                ("judge", "device", "TEXT NOT NULL DEFAULT ''"),
                                ("judge", "person", "TEXT NOT NULL DEFAULT ''"),
                                ("poster", "body", "TEXT NOT NULL DEFAULT ''"),
                                ("poster", "added_by", "INTEGER")):
            try:
                g.pj_db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:
                pass
        ensure_seats(g.pj_db)
        g.pj_db.commit()
    return g.pj_db


@bp.teardown_app_request
def _close_db(_exc):
    conn = g.pop('pj_db', None)
    if conn is not None:
        conn.close()


@bp.after_request
def _no_store(resp):
    resp.headers['Cache-Control'] = 'no-store'
    return resp


def now():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def get_setting(key, default=''):
    row = db().execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return row['value'] if row else default


def set_setting(key, value):
    db().execute("INSERT INTO setting (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def phase():
    # scoring -> finalists -> done
    return get_setting('phase', 'scoring')


def per_abstract_set():
    """The number the chief adjudicator typed."""
    return to_int(get_setting('per_poster', DEFAULT_PER_ABSTRACT), DEFAULT_PER_ABSTRACT)


def per_abstract():
    """Adjudicators needed per poster in practice: what was typed, but never more than there are people."""
    return max(1, min(per_abstract_set(), max(1, len(real_judges()))))


def me(api=False, need_name=True):
    """The signed-in person, from the session cookie. Not signed in: back to the sign-in page."""
    uid = session.get('pj_uid')
    row = db().execute("SELECT * FROM judge WHERE id = ?", (uid,)).fetchone() if uid else None
    if row and not row['is_admin'] and (not row['device'] or row['device'] != session.get('pj_dev')):
        row = None   # the number was freed or taken by another device
    if not row:
        session.pop('pj_uid', None)
        session.pop('pj_dev', None)
        if api:
            abort(make_response(jsonify(ok=False, error='Please sign in again.'), 401))
        abort(redirect(url_for('posters.index')))
    if need_name and not row['is_admin'] and not row['person']:
        if api:
            abort(make_response(jsonify(ok=False, error='Add your name first.'), 403))
        abort(redirect(url_for('posters.judge_home')))
    return row


def chair_only(judge):
    if not (judge['is_chair'] or judge['is_admin']):
        abort(403)


def say(msg):
    """One-shot message shown at the top of the next console page."""
    session['pj_flash'] = msg


def real_judges():
    """People who score and vote: everyone except an admin who is not also a chief adjudicator."""
    return db().execute("SELECT * FROM judge WHERE is_admin = 0 OR is_chair = 1 ORDER BY id").fetchall()


def on_panel(j):
    """True if this person scores and votes."""
    return not j['is_admin'] or bool(j['is_chair'])


def norm_code(raw):
    """Poster IDs are matched exactly, so ignore spaces and letter case when people type them."""
    return re.sub(r'\s+', '', (raw or '')).upper()


def to_int(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def self_add_on():
    return get_setting('self_add', '0') == '1'


# ---------- seats and the chief's password ----------

def seats():
    """The numbered adjudicator seats, in order."""
    return db().execute("SELECT * FROM judge WHERE is_admin = 0 ORDER BY id").fetchall()


def ensure_seats(conn):
    """Make sure the chief and the numbered seats exist. Runs on every connection and does nothing once set up."""
    if not conn.execute("SELECT 1 FROM judge WHERE is_admin = 1").fetchone():
        conn.execute("INSERT INTO judge (name, token, is_admin, is_chair) VALUES (?, ?, 1, 0)",
                     (CHIEF_NAME, secrets.token_urlsafe(12)))
    have = conn.execute("SELECT COUNT(*) c FROM judge WHERE is_admin = 0").fetchone()['c']
    row = conn.execute("SELECT value FROM setting WHERE key = 'panel_size'").fetchone()
    size = to_int(row['value'], DEFAULT_PANEL) if row else DEFAULT_PANEL
    if have == 0:
        for n in range(1, size + 1):
            conn.execute("INSERT INTO judge (name, token) VALUES (?, ?)", (f'Adjudicator {n}', secrets.token_urlsafe(12)))


def set_panel_size(n):
    """Grow or shrink the panel. Shrinking drops the highest numbers, unless they have already scored."""
    n = max(1, min(n, MAX_PANEL))
    d = db()
    rows = seats()
    if n > len(rows):
        for k in range(len(rows) + 1, n + 1):
            d.execute("INSERT INTO judge (name, token) VALUES (?, ?)", (f'Adjudicator {k}', secrets.token_urlsafe(12)))
    elif n < len(rows):
        drop = rows[n:]
        scored = [r['name'] for r in drop if d.execute(
            "SELECT 1 FROM score WHERE judge = ? AND submitted_at IS NOT NULL", (r['id'],)).fetchone()]
        if scored:
            say(f"{', '.join(scored)} already scored, so they cannot be removed.")
            return
        for r in drop:
            for t in ('assignment', 'score', 'vote', 'message'):
                d.execute(f"DELETE FROM {t} WHERE judge = ?", (r['id'],))
            d.execute("DELETE FROM judge WHERE id = ?", (r['id'],))
    set_setting('panel_size', n)


def set_person(jid, person):
    """Put a name next to a number: 'Adjudicator 3 · Jane Moyo'. Blank goes back to just the number."""
    person = re.sub(r'\s+', ' ', (person or '')).strip()[:40]
    d = db()
    row = d.execute("SELECT name FROM judge WHERE id = ? AND is_admin = 0", (jid,)).fetchone()
    m = re.match(r'Adjudicator (\d+)', row['name']) if row else None
    if not m:
        return
    d.execute("UPDATE judge SET person = ?, name = ? WHERE id = ?",
              (person, f'Adjudicator {m.group(1)}' + (f' · {person}' if person else ''), jid))


def chief_password():
    return get_setting('chief_password', DEFAULT_CHIEF_PASSWORD)


_bad = {}   # client -> recent wrong-password times (in memory, per process)


def too_many_tries():
    who = request.headers.get('CF-Connecting-IP') or request.remote_addr or ''
    cutoff = datetime.datetime.now().timestamp() - 600
    _bad[who] = [t for t in _bad.get(who, []) if t > cutoff]
    return who, len(_bad[who]) >= 8


# ---------- scoring maths ----------

def total_of(row):
    return sum(row[k] for k in CRIT_KEYS)


def ranking():
    """Posters ranked by mean total over submitted, non-conflicted reviews."""
    d = db()
    out = []
    for p in d.execute("SELECT * FROM poster ORDER BY code").fetchall():
        rows = d.execute(
            "SELECT s.* FROM score s JOIN assignment a ON a.poster = s.poster AND a.judge = s.judge "
            "WHERE s.poster = ? AND s.submitted_at IS NOT NULL AND a.conflict = 0", (p['code'],)).fetchall()
        assigned = d.execute("SELECT COUNT(*) c FROM assignment WHERE poster = ? AND conflict = 0",
                             (p['code'],)).fetchone()['c']
        n = len(rows)
        item = {'poster': p, 'n': n, 'assigned': assigned, 'mean': None, 'crit': {}}
        if n:
            item['mean'] = sum(total_of(r) for r in rows) / n
            item['crit'] = {k: sum(r[k] for r in rows) / n for k in CRIT_KEYS}
        out.append(item)
    out.sort(key=lambda i: (i['mean'] is None, -(i['mean'] or 0), i['poster']['code']))
    return out


def finalist_codes(rank, n_final):
    """Top n_final by mean. Anyone tied with the last place advances too."""
    scored = [i for i in rank if i['mean'] is not None]
    if len(scored) <= n_final:
        return [i['poster']['code'] for i in scored]
    cutoff = round(scored[n_final - 1]['mean'], 4)
    return [i['poster']['code'] for i in scored if round(i['mean'], 4) >= cutoff]


def progress():
    """Per person: how many of their assigned posters are submitted / conflicted."""
    d = db()
    rows = []
    for j in real_judges():
        a = d.execute("SELECT conflict FROM assignment WHERE judge = ?", (j['id'],)).fetchall()
        done = d.execute(
            "SELECT COUNT(*) c FROM score s JOIN assignment a ON a.poster = s.poster AND a.judge = s.judge "
            "WHERE s.judge = ? AND s.submitted_at IS NOT NULL AND a.conflict = 0", (j['id'],)).fetchone()['c']
        conflicts = sum(1 for x in a if x['conflict'])
        rows.append({'judge': j, 'total': len(a) - conflicts, 'done': done, 'conflicts': conflicts})
    return rows


def auto_assign(per_poster, only=None):
    """Share the posters out fairly. Every poster gets `per_poster` adjudicators, picked so that
      1. each adjudicator gets a fair share of EACH theme (nobody is left with one department only),
      2. each adjudicator has about the same total number of posters, and
      3. pairings are spread, so every adjudicator is cross-checked against as many different colleagues
         as possible rather than always the same one.
    Existing assignments and declared conflicts are kept; only gaps are filled, one slot at a time."""
    d = db()
    judges = [r['id'] for r in real_judges()]
    if not judges:
        return 0
    random.shuffle(judges)   # still balanced, but not the same split every time
    posters = d.execute("SELECT code, theme FROM poster ORDER BY theme, code").fetchall()
    if only:
        posters = [p for p in posters if p['code'] == only]
    theme_of = {p['code']: (p['theme'] or '') for p in posters}
    load = {j: 0 for j in judges}
    tload = {}
    for r in d.execute("SELECT poster, judge FROM assignment WHERE conflict = 0"):
        if r['judge'] in load:
            load[r['judge']] += 1
            k = (r['judge'], theme_of.get(r['poster'], ''))
            tload[k] = tload.get(k, 0) + 1
    paired = {}   # how often two adjudicators already share a poster

    def pk(a, b):
        return (a, b) if a < b else (b, a)
    on_poster = {}
    for r in d.execute("SELECT poster, judge FROM assignment WHERE conflict = 0"):
        on_poster.setdefault(r['poster'], []).append(r['judge'])
    for js in on_poster.values():
        for x in range(len(js)):
            for y in range(x + 1, len(js)):
                paired[pk(js[x], js[y])] = paired.get(pk(js[x], js[y]), 0) + 1
    added = 0
    for i, p in enumerate(posters):
        theme = p['theme'] or ''
        have = {r['judge']: r['conflict'] for r in
                d.execute("SELECT judge, conflict FROM assignment WHERE poster = ?", (p['code'],))}
        need = per_poster - sum(1 for c in have.values() if not c)
        for _ in range(max(need, 0)):
            free = [j for j in judges if j not in have]
            if not free:
                break
            mates = [h for h, c in have.items() if not c]
            pick = min(free, key=lambda j: (tload.get((j, theme), 0), load[j],
                                            sum(paired.get(pk(j, h), 0) for h in mates),
                                            (judges.index(j) - i) % len(judges)))
            for h in mates:
                paired[pk(pick, h)] = paired.get(pk(pick, h), 0) + 1
            d.execute("INSERT INTO assignment (poster, judge) VALUES (?, ?)", (p['code'], pick))
            have[pick] = 0
            load[pick] += 1
            tload[(pick, theme)] = tload.get((pick, theme), 0) + 1
            added += 1
    return added


def board_state():
    """Everything the assignment board needs, as plain data: posters with who is on them, adjudicators with
    their counts per theme, and how many adjudicators each poster needs."""
    d = db()
    judges = real_judges()
    names = {j['id']: j['name'] for j in judges}
    posters = d.execute("SELECT code, title, COALESCE(NULLIF(theme, ''), 'No theme') AS theme "
                        "FROM poster ORDER BY theme, code").fetchall()
    on = {}
    for r in d.execute("SELECT a.poster, a.judge, a.conflict, (s.submitted_at IS NOT NULL) AS scored "
                       "FROM assignment a LEFT JOIN score s ON s.poster = a.poster AND s.judge = a.judge "
                       "ORDER BY a.judge"):
        if r['judge'] in names:
            on.setdefault(r['poster'], []).append({'id': r['judge'], 'name': names[r['judge']],
                                                   'conflict': bool(r['conflict']), 'scored': bool(r['scored'])})
    themes = list(dict.fromkeys(p['theme'] for p in posters))
    by = {j['id']: {t: 0 for t in themes} for j in judges}
    for p in posters:
        for a in on.get(p['code'], []):
            if not a['conflict']:
                by[a['id']][p['theme']] += 1
    return {'n': per_abstract(), 'themes': themes,
            'sizes': {t: sum(1 for p in posters if p['theme'] == t) for t in themes},
            'judges': [{'id': j['id'], 'name': j['name'], 'by': by[j['id']], 'total': sum(by[j['id']].values())}
                       for j in judges],
            'posters': [{'code': p['code'], 'title': p['title'], 'theme': p['theme'],
                         'assigned': on.get(p['code'], [])} for p in posters]}


def share_table():
    """How many posters of each theme every adjudicator has, to show the sharing is fair."""
    d = db()
    themes = [r['t'] for r in d.execute("SELECT DISTINCT COALESCE(NULLIF(theme, ''), 'No theme') t FROM poster ORDER BY t")]
    rows = {j['id']: {'judge': j, 'by': {t: 0 for t in themes}, 'total': 0} for j in real_judges()}
    for r in d.execute("SELECT a.judge, COALESCE(NULLIF(p.theme, ''), 'No theme') t FROM assignment a "
                       "JOIN poster p ON p.code = a.poster WHERE a.conflict = 0"):
        if r['judge'] in rows:
            rows[r['judge']]['by'][r['t']] += 1
            rows[r['judge']]['total'] += 1
    lst = list(rows.values())
    spread = {t: (min(x['by'][t] for x in lst), max(x['by'][t] for x in lst)) for t in themes} if lst else {}
    totals = (min(x['total'] for x in lst), max(x['total'] for x in lst)) if lst else (0, 0)
    sizes = {t: d.execute("SELECT COUNT(*) c FROM poster WHERE COALESCE(NULLIF(theme, ''), 'No theme') = ?", (t,)).fetchone()['c']
             for t in themes}
    fair = bool(lst) and all(hi - lo <= 1 for lo, hi in spread.values()) and totals[1] - totals[0] <= 2
    return {'themes': themes, 'rows': lst, 'spread': spread, 'totals': totals, 'sizes': sizes, 'fair': fair}


# ---------- final vote ----------

def round_row(n):
    return db().execute("SELECT * FROM vote_round WHERE n = ?", (n,)).fetchone()


def cur_round():
    return int(get_setting('vote_round', '1'))


def vote_state():
    """Everything the finalist screens need about the current and past voting rounds."""
    d = db()
    rnd = cur_round()
    row = round_row(rnd)
    cands = row['candidates'].split(',') if row else []
    votes = d.execute("SELECT v.*, j.name FROM vote v JOIN judge j ON j.id = v.judge "
                      "WHERE v.round = ? ORDER BY v.updated_at", (rnd,)).fetchall()
    counts = {}
    for v in votes:
        counts[v['poster']] = counts.get(v['poster'], 0) + 1
    by_poster = {}
    for v in votes:
        by_poster.setdefault(v['poster'], []).append(v)
    voted = {v['judge'] for v in votes}
    conflicted = {}
    for a in d.execute("SELECT poster, judge FROM assignment WHERE conflict = 1"):
        conflicted.setdefault(a['judge'], set()).add(a['poster'])
    missing = [j['name'] for j in real_judges()
               if j['id'] not in voted and set(cands) - conflicted.get(j['id'], set())]
    history = []
    for r in d.execute("SELECT * FROM vote_round WHERE closed = 1 ORDER BY n").fetchall():
        c = {}
        for v in d.execute("SELECT poster FROM vote WHERE round = ?", (r['n'],)):
            c[v['poster']] = c.get(v['poster'], 0) + 1
        history.append({'n': r['n'], 'cands': r['candidates'].split(','), 'counts': c, 'outcome': r['outcome']})
    return {'round': rnd, 'cands': cands, 'closed': bool(row and row['closed']), 'votes': votes,
            'counts': counts, 'by_poster': by_poster, 'missing': missing, 'history': history,
            'casting': get_setting('casting') == '1', 'winner': get_setting('winner'),
            'casting_reason': get_setting('casting_reason'), 'n_voters': len(real_judges())}


def auto_count():
    """Count the round the moment the last adjudicator has voted. A tie opens the next round by itself."""
    if phase() != 'finalists':
        return
    vs = vote_state()
    if not vs['closed'] and vs['votes'] and not vs['missing']:
        count_votes()


def count_votes():
    """Close the current round. One clear leader wins; a tie opens a new round between the leaders."""
    d = db()
    rnd = cur_round()
    row = round_row(rnd)
    if not row or row['closed']:
        return 'This round is already counted.'
    cands = row['candidates'].split(',')
    counts = {c: 0 for c in cands}
    for v in d.execute("SELECT poster FROM vote WHERE round = ?", (rnd,)):
        if v['poster'] in counts:
            counts[v['poster']] += 1
    if not any(counts.values()):
        return 'No votes yet.'
    top = max(counts.values())
    leaders = [c for c in cands if counts[c] == top]
    if len(leaders) == 1:
        d.execute("UPDATE vote_round SET closed = 1, outcome = ? WHERE n = ?", ('winner:' + leaders[0], rnd))
        set_setting('winner', leaders[0])
        set_setting('phase', 'done')
        return ''
    d.execute("UPDATE vote_round SET closed = 1, outcome = ? WHERE n = ?", ('tie:' + ','.join(leaders), rnd))
    d.execute("INSERT INTO vote_round (n, candidates) VALUES (?, ?)", (rnd + 1, ','.join(leaders)))
    set_setting('vote_round', rnd + 1)
    return ''


# ---------- entry ----------

@bp.route('/')
def index():
    d = db()
    uid = session.get('pj_uid')
    j = d.execute("SELECT * FROM judge WHERE id = ?", (uid,)).fetchone() if uid else None
    if j and not request.args.get('all') and (j['is_admin'] or (j['device'] and j['device'] == session.get('pj_dev'))):
        return redirect(url_for('posters.chair' if j['is_admin'] else 'posters.judge_home'))
    back = None
    if j and j['is_admin']:
        back = (url_for('posters.chair'), 'Back to the console')
    elif j and j['device'] and j['device'] == session.get('pj_dev'):
        back = (url_for('posters.judge_home'), 'Back to my posters')
    return render_template('landing.html', seats=seats(), mine=session.get('pj_dev'), error=session.pop('pj_flash', ''), back=back)


@bp.route('/pick/<int:n>', methods=['POST'])
def pick(n):
    rows = seats()
    if not 1 <= n <= len(rows):
        abort(404)
    seat = rows[n - 1]
    mine = session.get('pj_dev') or secrets.token_urlsafe(16)
    d = db()
    held = d.execute("SELECT id FROM judge WHERE device = ? AND device != ''", (mine,)).fetchone()
    if held and held['id'] != seat['id']:
        return redirect(url_for('posters.judge_home'))   # one number per device
    cur = d.execute("UPDATE judge SET device = ? WHERE id = ? AND (device = '' OR device = ?)", (mine, seat['id'], mine))
    d.commit()
    if not cur.rowcount:
        say(f'Number {n} is already taken. Pick another, or ask the chief adjudicator.')
        return redirect(url_for('posters.index'))
    session.permanent = True
    session['pj_uid'] = seat['id']
    session['pj_dev'] = mine
    return redirect(url_for('posters.judge_home'))


@bp.route('/chief', methods=['POST'])
def chief_signin():
    who, locked = too_many_tries()
    given = request.form.get('password', '')
    if locked:
        say('Too many tries. Wait a few minutes.')
        return redirect(url_for('posters.index'))
    if not hmac.compare_digest(given.encode('utf-8'), chief_password().encode('utf-8')):
        _bad.setdefault(who, []).append(datetime.datetime.now().timestamp())
        say('That password is not right.')
        return redirect(url_for('posters.index'))
    uid, dev = session.get('pj_uid'), session.get('pj_dev')
    holds = bool(uid and dev and db().execute("SELECT 1 FROM judge WHERE id = ? AND device = ? AND is_admin = 0", (uid, dev)).fetchone())
    row = db().execute("SELECT id FROM judge WHERE is_admin = 1 ORDER BY id LIMIT 1").fetchone()
    session.permanent = True
    if holds:
        session['pj_seat'] = uid   # this device keeps its number; signing out of the console returns to it
    else:
        session.pop('pj_dev', None)
        session.pop('pj_seat', None)
    session['pj_uid'] = row['id']
    return redirect(url_for('posters.chair'))


@bp.route('/logout')
def logout():
    """Chief signs out. An adjudicator who changes number gives theirs up for someone else."""
    uid = session.get('pj_uid')
    dev = session.get('pj_dev')
    seat = session.pop('pj_seat', None)
    if seat and seat != uid and dev and db().execute("SELECT 1 FROM judge WHERE id = ? AND device = ?", (seat, dev)).fetchone():
        session['pj_uid'] = seat   # back from the console to this device's own number
        return redirect(url_for('posters.judge_home'))
    if uid and dev:
        if db().execute("UPDATE judge SET device = '' WHERE id = ? AND device = ?", (uid, dev)).rowcount:
            set_person(uid, '')   # the next person on this number starts without the old name
        db().commit()
    session.pop('pj_uid', None)
    session.pop('pj_dev', None)
    return redirect(url_for('posters.index'))


@bp.route('/my/name', methods=['POST'])
def my_name():
    j = me(need_name=False)
    if not on_panel(j):
        abort(403)
    person = re.sub(r'\s+', ' ', request.form.get('person', '')).strip()
    if not person:
        say('Type your name to continue.')
    else:
        set_person(j['id'], person)
        db().commit()
    return redirect(url_for('posters.judge_home'))


@bp.route('/home')
def judge_home():
    j = me(need_name=False)
    if not on_panel(j):
        return redirect(url_for('posters.chair'))
    if not j['person']:
        return render_template('name.html', j=j, flash_msg=session.pop('pj_flash', ''))
    d = db()
    rows = d.execute(
        "SELECT p.*, a.conflict, s.submitted_at, s.meth, s.evid, s.nov, s.rel, s.clar, "
        "(SELECT COUNT(*) FROM score s2 WHERE s2.poster = p.code AND s2.submitted_at IS NOT NULL) AS n_sub "
        "FROM assignment a JOIN poster p ON p.code = a.poster "
        "LEFT JOIN score s ON s.poster = a.poster AND s.judge = a.judge "
        "WHERE a.judge = ? ORDER BY p.code", (j['id'],)).fetchall()
    items = []
    for r in rows:
        if r['conflict']:
            status = 'conflict'
        elif r['submitted_at']:
            status = 'done'
        elif any(r[k] is not None for k in CRIT_KEYS):
            status = 'draft'
        else:
            status = 'todo'
        items.append({'p': r, 'status': status, 'total': total_of(r) if status == 'done' else None})
    active = [i for i in items if i['status'] != 'conflict']
    done = sum(1 for i in active if i['status'] == 'done')
    vs = vote_state() if phase() != 'scoring' else None
    voted = bool(vs) and any(v['judge'] == j['id'] for v in vs['votes'])
    return render_template('judge_home.html', j=j, items=items, done=done, total=len(active),
                           phase=phase(), vs=vs, voted=voted, self_add=self_add_on(),
                           flash_msg=session.pop('pj_flash', ''))


@bp.route('/my/add-abstract', methods=['POST'])
def my_add_abstract():
    """An adjudicator adds a poster they are reviewing, by its ID. If another adjudicator already
    added that ID, they simply join it, so the same poster never exists twice."""
    j = me()
    if not on_panel(j):
        abort(403)
    d = db()
    code, title = norm_code(request.form.get('code')), request.form.get('title', '').strip()
    if phase() != 'scoring' or not self_add_on():
        say('Adding posters is switched off. Ask the chief adjudicator to assign them to you.')
        return redirect(url_for('posters.judge_home'))
    if not code:
        say('Type the poster ID first.')
        return redirect(url_for('posters.judge_home'))
    existing = d.execute("SELECT * FROM poster WHERE code = ?", (code,)).fetchone()
    if existing is None:
        if not title:
            say('This ID is new, so please type its title too.')
            return redirect(url_for('posters.judge_home'))
        d.execute("INSERT INTO poster (code, title, presenter, theme, body, added_by) VALUES (?, ?, '', '', ?, ?)",
                  (code, title, request.form.get('body', '').strip(), j['id']))
    mine = d.execute("SELECT conflict FROM assignment WHERE poster = ? AND judge = ?", (code, j['id'])).fetchone()
    if mine and mine['conflict']:
        say(f'You declared a conflict on {code}.')
        return redirect(url_for('posters.judge_home'))
    d.execute("INSERT OR IGNORE INTO assignment (poster, judge) VALUES (?, ?)", (code, j['id']))
    d.commit()
    return redirect(url_for('posters.score_page', code=code))


@bp.route('/my/withdraw/<code>', methods=['POST'])
def my_withdraw(code):
    """Fix a typo: remove a poster you added, as long as nobody has scored it yet."""
    j = me()
    d = db()
    p = d.execute("SELECT * FROM poster WHERE code = ?", (code,)).fetchone()
    scored = d.execute("SELECT COUNT(*) c FROM score WHERE poster = ? AND submitted_at IS NOT NULL", (code,)).fetchone()['c']
    if p and p['added_by'] == j['id'] and not scored and phase() == 'scoring':
        for t in ('assignment', 'score', 'message'):
            d.execute(f"DELETE FROM {t} WHERE poster = ?", (code,))
        d.execute("DELETE FROM poster WHERE code = ?", (code,))
        d.commit()
        say(f'{code} was removed.')
    else:
        say('That poster cannot be removed: someone has already scored it, or you did not add it.')
    return redirect(url_for('posters.judge_home'))


# ---------- round 1: scoring ----------

def _assignment_or_403(j, code):
    a = db().execute("SELECT * FROM assignment WHERE poster = ? AND judge = ?", (code, j['id'])).fetchone()
    if not a:
        abort(403)
    return a


@bp.route('/score/<code>')
def score_page(code):
    j = me()
    a = _assignment_or_403(j, code)
    d = db()
    p = d.execute("SELECT * FROM poster WHERE code = ?", (code,)).fetchone()
    s = d.execute("SELECT * FROM score WHERE poster = ? AND judge = ?", (code, j['id'])).fetchone()
    codes = [r['poster'] for r in d.execute(
        "SELECT poster FROM assignment WHERE judge = ? ORDER BY poster", (j['id'],))]
    i = codes.index(code)
    return render_template('score.html', j=j, p=p, s=s, a=a, criteria=CRITERIA, phase=phase(),
                           prev=codes[i - 1] if i > 0 else None,
                           nxt=codes[i + 1] if i < len(codes) - 1 else None)


@bp.route('/score/<code>/save', methods=['POST'])
def score_save(code):
    j = me(api=True)
    a = _assignment_or_403(j, code)
    if phase() != 'scoring':
        return jsonify(ok=False, error='Scoring is closed.'), 409
    d = db()
    data = request.get_json(silent=True) or {}

    if 'conflict' in data:
        d.execute("UPDATE assignment SET conflict = ? WHERE poster = ? AND judge = ?",
                  (1 if data['conflict'] else 0, code, j['id']))
        if data['conflict']:
            d.execute("DELETE FROM score WHERE poster = ? AND judge = ?", (code, j['id']))
            auto_assign(per_abstract(), only=code)   # quietly hand it to someone else
        d.commit()
        return jsonify(ok=True)
    if a['conflict']:
        return jsonify(ok=False, error='You declared a conflict on this poster.'), 409

    d.execute("INSERT OR IGNORE INTO score (poster, judge) VALUES (?, ?)", (code, j['id']))
    for k in CRIT_KEYS:
        if k in data:
            v = data[k]
            if v is None or v == '':
                d.execute(f"UPDATE score SET {k} = NULL WHERE poster = ? AND judge = ?", (code, j['id']))
                continue
            try:
                v = int(v)
            except (TypeError, ValueError):
                return jsonify(ok=False, error='Marks must be whole numbers.'), 400
            if not 0 <= v <= CRIT_MAX[k]:
                return jsonify(ok=False, error=f'{k} must be 0 to {CRIT_MAX[k]}.'), 400
            d.execute(f"UPDATE score SET {k} = ? WHERE poster = ? AND judge = ?", (v, code, j['id']))
    if 'note' in data:
        d.execute("UPDATE score SET note = ? WHERE poster = ? AND judge = ?",
                  (str(data['note'])[:2000], code, j['id']))

    row = d.execute("SELECT * FROM score WHERE poster = ? AND judge = ?", (code, j['id'])).fetchone()
    complete = all(row[k] is not None for k in CRIT_KEYS)
    if data.get('submit'):
        if not complete:
            return jsonify(ok=False, error='Score all five criteria before submitting.'), 400
        d.execute("UPDATE score SET submitted_at = ? WHERE poster = ? AND judge = ?", (now(), code, j['id']))
    elif row['submitted_at'] and not complete:
        d.execute("UPDATE score SET submitted_at = NULL WHERE poster = ? AND judge = ?", (code, j['id']))
    d.commit()
    return jsonify(ok=True, complete=complete, total=total_of(row) if complete else None)


# ---------- chair console ----------

def _needs_assign(posters, ass):
    n = per_abstract()
    return [p for p in posters if sum(1 for x in ass.get(p['code'], []) if not x['conflict']) < n]


@bp.route('/chair')
def chair():
    j = me()
    chair_only(j)
    d = db()
    judges = real_judges()
    posters = d.execute("SELECT p.*, j.name AS added_name FROM poster p "
                        "LEFT JOIN judge j ON j.id = p.added_by ORDER BY p.code").fetchall()
    ass = {}
    for r in d.execute("SELECT a.poster, a.judge, a.conflict, j.name FROM assignment a "
                       "JOIN judge j ON j.id = a.judge ORDER BY j.name"):
        ass.setdefault(r['poster'], []).append(r)
    ph = phase()
    n_req = per_abstract()
    n_final = int(get_setting('n_finalists', DEFAULT_FINALISTS))
    rank = ranking()
    needs_assign = _needs_assign(posters, ass)
    covered = sum(1 for i in rank if i['n'] >= n_req)
    ready = bool(posters) and covered == len(posters)
    preview_codes = finalist_codes(rank, n_final)
    scored = [i for i in rank if i['mean'] is not None]
    cutoff_tie = len(scored) > n_final and len(preview_codes) > n_final
    submitted = {(r['poster'], r['judge']) for r in d.execute(
        "SELECT poster, judge FROM score WHERE submitted_at IS NOT NULL")}
    waiting = {p['code']: [x['name'] for x in ass.get(p['code'], [])
                           if not x['conflict'] and (p['code'], x['judge']) not in submitted] for p in posters}

    if len(judges) < 2:
        default_step = 1
    elif not posters:
        default_step = 2
    elif needs_assign:
        default_step = 3
    elif ph == 'scoring':
        default_step = 4
    else:
        default_step = 5
    step = request.args.get('step', type=int)
    if step not in (1, 2, 3, 4, 5):
        step = default_step

    return render_template(
        'chair.html', j=j, step=step, n_set=per_abstract_set(), n_panel=len(judges), base_url=PUBLIC_BASE + '/', self_add=self_add_on(), share=share_table(), board=board_state(), default_step=default_step, judges=judges, posters=posters, ass=ass,
        rank=rank, progress=progress(), phase=ph, n_req=n_req, n_final=n_final, covered=covered, ready=ready,
        preview=preview_codes, cutoff_tie=cutoff_tie, needs_assign=needs_assign, waiting=waiting,
        vs=vote_state() if ph != 'scoring' else None, criteria=CRITERIA,
        flash_msg=session.pop('pj_flash', ''))


@bp.route('/chair/<action>', methods=['POST'])
def chair_action(action):
    j = me()
    chair_only(j)
    d = db()
    f = request.form
    step = f.get('step', type=int)
    back = redirect(url_for('posters.chair', step=step) if step else url_for('posters.chair'))
    jid = int(f['judge']) if f.get('judge', '').isdigit() else None
    target = d.execute("SELECT * FROM judge WHERE id = ?", (jid,)).fetchone() if jid else None

    if action == 'set-panel':
        if phase() != 'scoring':
            say('The panel is locked once the final vote has started.')
        else:
            set_panel_size(to_int(f.get('size'), DEFAULT_PANEL))
    elif action == 'set-name' and jid:
        set_person(jid, f.get('person', ''))
    elif action == 'free-seat' and jid:
        d.execute("UPDATE judge SET device = '' WHERE id = ? AND is_admin = 0", (jid,))
        set_person(jid, '')
    elif action == 'free-all':
        d.execute("UPDATE judge SET device = '' WHERE is_admin = 0")
        for r in seats():
            set_person(r['id'], '')
    elif action == 'set-password':
        new = f.get('password', '').strip()
        if len(new) < 4:
            say('Use at least 4 characters.')
        else:
            set_setting('chief_password', new)
            say('Password changed.')
    elif action == 'reset-all':
        if f.get('confirm', '').strip().upper() == 'CLEAR':
            for t in ('assignment', 'score', 'message', 'vote', 'vote_round'):
                d.execute(f"DELETE FROM {t}")
            d.execute("UPDATE poster SET finalist = 0")
            d.execute("DELETE FROM setting WHERE key IN ('winner', 'casting', 'casting_reason', 'vote_round', 'phase')")
            say('All scores, votes and assignments were cleared. The posters and adjudicator numbers are kept.')
        else:
            say('Nothing was cleared. Type CLEAR in the box to confirm.')
    elif action == 'add-abstract':
        title = f.get('title', '').strip()
        code = norm_code(f.get('code'))
        if not title:
            say('Type the poster title first.')
        else:
            if not code:
                nums = [int(r['code'][4:]) for r in d.execute("SELECT code FROM poster WHERE code LIKE 'ABS-%'")
                        if r['code'][4:].isdigit()]
                code = f"ABS-{max(nums, default=0) + 1:03d}"
            if d.execute("SELECT 1 FROM poster WHERE code = ?", (code,)).fetchone():
                say(f'{code} already exists. Use a different code.')
            else:
                d.execute("INSERT INTO poster (code, title, presenter, theme, body) VALUES (?, ?, ?, ?, ?)",
                          (code, title, f.get('presenter', '').strip(), f.get('theme', '').strip(),
                           f.get('body', '').strip()))
    elif action == 'import-abstracts':
        for line in f.get('rows', '').splitlines():
            if not line.strip():
                continue
            parts = [x.strip() for x in line.split('\t' if '\t' in line else '|')]
            if len(parts) < 2 or parts[0].lower() == 'code':
                continue
            parts += [''] * (5 - len(parts))
            parts[0] = norm_code(parts[0])
            parts[4] = parts[4].replace('\\n', '\n')
            if not parts[0]:
                continue
            d.execute("INSERT INTO poster (code, title, presenter, theme, body) VALUES (?, ?, ?, ?, ?) "
                      "ON CONFLICT(code) DO UPDATE SET title = excluded.title, presenter = excluded.presenter, "
                      "theme = excluded.theme, body = excluded.body",
                      (parts[0], parts[1], parts[2], parts[3], parts[4]))
    elif action == 'set-self-add':
        set_setting('self_add', '1' if f.get('on') == '1' else '0')
    elif action == 'delete-abstract':
        if phase() != 'scoring':
            say('Posters cannot be deleted once the final vote has started.')
        else:
            code = f['poster']
            for t in ('assignment', 'score', 'message'):
                d.execute(f"DELETE FROM {t} WHERE poster = ?", (code,))
            d.execute("DELETE FROM poster WHERE code = ?", (code,))
    elif action == 'auto-assign':
        k = max(1, min(to_int(f.get('per_poster'), DEFAULT_PER_ABSTRACT), 20))
        set_setting('per_poster', k)
        auto_assign(per_abstract())
    elif action == 'set-per':
        set_setting('per_poster', max(1, min(to_int(f.get('per_poster'), DEFAULT_PER_ABSTRACT), 20)))
    elif action == 'assign' and jid:
        d.execute("INSERT OR IGNORE INTO assignment (poster, judge) VALUES (?, ?)", (f['poster'], jid))
    elif action == 'unassign' and jid:
        d.execute("DELETE FROM assignment WHERE poster = ? AND judge = ?", (f['poster'], jid))
        d.execute("DELETE FROM score WHERE poster = ? AND judge = ?", (f['poster'], jid))
    elif action == 'set-finalists':
        set_setting('n_finalists', max(1, min(to_int(f.get('n'), DEFAULT_FINALISTS), 20)))
    elif action == 'start-final':
        rank = ranking()
        n_req = per_abstract()
        short = [i for i in rank if i['n'] < n_req]
        if phase() != 'scoring':
            pass
        elif not rank or short:
            say(f'{len(short)} poster{"" if len(short) == 1 else "s"} still need more scores before the '
                f'final vote can start.')
        else:
            codes = finalist_codes(rank, int(get_setting('n_finalists', DEFAULT_FINALISTS)))
            d.execute("UPDATE poster SET finalist = 0")
            for c in codes:
                d.execute("UPDATE poster SET finalist = 1 WHERE code = ?", (c,))
            d.execute("DELETE FROM vote_round")
            d.execute("DELETE FROM vote")
            d.execute("DELETE FROM setting WHERE key IN ('winner', 'casting', 'casting_reason')")
            d.execute("INSERT INTO vote_round (n, candidates) VALUES (1, ?)", (','.join(codes),))
            set_setting('vote_round', 1)
            set_setting('phase', 'finalists')
    elif action == 'count-votes':
        if phase() == 'finalists':
            msg = count_votes()
            if msg:
                say(msg)
    elif action == 'cast-vote':
        vs = vote_state()
        code, reason = f.get('winner', ''), f.get('reason', '').strip()
        if phase() == 'finalists' and vs['casting'] and code in vs['cands'] and reason:
            set_setting('winner', code)
            set_setting('casting_reason', reason[:3000])
            set_setting('phase', 'done')
        else:
            say('Choose the winner and give a reason.')
    elif action == 'reopen-scoring':
        d.execute("UPDATE poster SET finalist = 0")
        d.execute("DELETE FROM vote")
        d.execute("DELETE FROM vote_round")
        d.execute("DELETE FROM message")
        d.execute("DELETE FROM setting WHERE key IN ('winner', 'casting', 'casting_reason', 'vote_round')")
        set_setting('phase', 'scoring')
    d.commit()
    return back


@bp.route('/chair/board/<op>', methods=['POST'])
def chair_board(op):
    """Pick-and-drop assignment: assign posters to an adjudicator, take one off, or clear the lot."""
    j = me(api=True)
    chair_only(j)
    if op not in ('assign', 'unassign', 'clear'):
        abort(404)
    if phase() != 'scoring':
        return jsonify(ok=False, error='Scoring has started, so assignments are locked.'), 409
    d = db()
    data = request.get_json(silent=True) or {}
    jid = data.get('judge')
    if op in ('assign', 'unassign') and jid not in {r['id'] for r in real_judges()}:
        return jsonify(ok=False, error='Choose an adjudicator.'), 400
    if op == 'assign':
        known = {r['code'] for r in d.execute("SELECT code FROM poster")}
        codes = [c for c in (data.get('posters') or []) if isinstance(c, str) and c in known]
        if not codes:
            return jsonify(ok=False, error='Pick at least one poster first.'), 400
        for c in codes:
            d.execute("INSERT OR IGNORE INTO assignment (poster, judge) VALUES (?, ?)", (c, jid))
    elif op == 'unassign':
        code = data.get('poster')
        scored = d.execute("SELECT 1 FROM score WHERE poster = ? AND judge = ? AND submitted_at IS NOT NULL",
                           (code, jid)).fetchone()
        if scored and not data.get('force'):
            return jsonify(ok=False, needs_confirm=True,
                           error='This adjudicator has already scored this poster. Removing them deletes that score.'), 409
        d.execute("DELETE FROM assignment WHERE poster = ? AND judge = ?", (code, jid))
        d.execute("DELETE FROM score WHERE poster = ? AND judge = ?", (code, jid))
    else:
        if d.execute("SELECT 1 FROM score WHERE submitted_at IS NOT NULL").fetchone():
            return jsonify(ok=False, error='Some posters are already scored, so everything cannot be cleared.'), 409
        d.execute("DELETE FROM assignment WHERE conflict = 0")
        d.execute("DELETE FROM score WHERE submitted_at IS NULL")
    d.commit()
    return jsonify(ok=True, state=board_state())


# ---------- final vote: adjudicator screens ----------

@bp.route('/finalists')
def finalists():
    j = me()
    d = db()
    if phase() == 'scoring':
        return redirect(url_for('posters.judge_home'))
    vs = vote_state()
    rank = {i['poster']['code']: i for i in ranking()}
    rank_list = [i for i in rank.values() if i['poster']['finalist']]
    msgs = {}
    for m in d.execute("SELECT m.*, j.name FROM message m JOIN judge j ON j.id = m.judge ORDER BY m.id"):
        msgs.setdefault(m['poster'], []).append(m)
    notes = {}
    for n in d.execute("SELECT s.poster, s.note, j.name FROM score s JOIN judge j ON j.id = s.judge "
                       "WHERE s.submitted_at IS NOT NULL AND s.note != '' ORDER BY j.name"):
        notes.setdefault(n['poster'], []).append(n)
    conflicts = {r['poster'] for r in d.execute(
        "SELECT poster FROM assignment WHERE judge = ? AND conflict = 1", (j['id'],))}
    mine = next((v for v in vs['votes'] if v['judge'] == j['id']), None)
    is_voter = on_panel(j)
    can_see_votes = (not is_voter) or bool(mine) or phase() == 'done' or vs['closed']
    cur = [rank[c] for c in vs['cands'] if c in rank]
    out = [i for i in rank_list if i['poster']['code'] not in vs['cands']]
    return render_template('finalists.html', j=j, vs=vs, cur=cur, out=out, criteria=CRITERIA, msgs=msgs,
                           notes=notes, conflicts=conflicts, mine=mine, is_voter=is_voter,
                           can_see_votes=can_see_votes, phase=phase(), rank=rank)


@bp.route('/finalists/message', methods=['POST'])
def finalist_message():
    j = me()
    d = db()
    code, body = request.form.get('poster', ''), request.form.get('body', '').strip()
    if phase() == 'finalists' and body and on_panel(j) and d.execute(
            "SELECT 1 FROM poster WHERE code = ? AND finalist = 1", (code,)).fetchone():
        d.execute("INSERT INTO message (poster, judge, body, created_at) VALUES (?, ?, ?, ?)",
                  (code, j['id'], body[:2000], now()))
        d.commit()
    return redirect(url_for('posters.finalists') + f'#p-{code}')


@bp.route('/finalists/vote', methods=['POST'])
def finalist_vote():
    j = me()
    d = db()
    code, reason = request.form.get('poster', ''), request.form.get('reason', '').strip()
    vs = vote_state()
    ok = (phase() == 'finalists' and on_panel(j) and reason and not vs['closed'] and code in vs['cands'] and
          not d.execute("SELECT 1 FROM assignment WHERE poster = ? AND judge = ? AND conflict = 1",
                        (code, j['id'])).fetchone())
    if ok:
        d.execute("INSERT INTO vote (judge, round, poster, reason, updated_at) VALUES (?, ?, ?, ?, ?) "
                  "ON CONFLICT(judge, round) DO UPDATE SET poster = excluded.poster, "
                  "reason = excluded.reason, updated_at = excluded.updated_at",
                  (j['id'], vs['round'], code, reason[:3000], now()))
        auto_count()
        d.commit()
    return redirect(url_for('posters.finalists') + f'#p-{code}')
