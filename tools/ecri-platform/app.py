import os
import re
import sqlite3
import datetime
import io
import base64
import smtplib
import threading
import time as time_module
import zipfile
from email.message import EmailMessage
from functools import wraps

import qrcode
from urllib.parse import urlsplit, urlunsplit
from PIL import Image, UnidentifiedImageError
from flask import Flask, request, jsonify, render_template, g, session, redirect, url_for, send_from_directory, has_request_context
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.security import generate_password_hash, check_password_hash
from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.lib.utils import ImageReader

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'ecri.db')
SECRET_KEY_PATH = os.path.join(BASE_DIR, 'secret_key')
LOGO_PATH = os.path.join(BASE_DIR, 'static', 'ceshhar-logo.png')

SMTP_FROM = 'admin@example.org'
SMTP_PASS = os.environ.get('SMTP_APP_PASSWORD', '')

LINK_MAX_AGE = 60 * 60 * 48  # 48 hours

# Where emailed links point. To move domains, write the new origin (e.g. https://ecri.online) into the file
# 'public_origin' next to this script and restart.
_origin_file = os.path.join(BASE_DIR, 'public_origin')
PUBLIC_BASE = (open(_origin_file).read().strip().rstrip('/') if os.path.exists(_origin_file)
               else 'https://ecri.online')


def get_secret_key():
    if os.path.exists(SECRET_KEY_PATH):
        with open(SECRET_KEY_PATH) as f:
            return f.read().strip()
    key = os.urandom(32).hex()
    with open(SECRET_KEY_PATH, 'w') as f:
        f.write(key)
    os.chmod(SECRET_KEY_PATH, 0o600)
    return key


SECRET_KEY = get_secret_key()
_serializer = URLSafeTimedSerializer(SECRET_KEY, salt='ecri-my-submissions')
_staff_serializer = URLSafeTimedSerializer(SECRET_KEY, salt='ecri-staff-pw')


def make_link_token(email):
    return _serializer.dumps(email.lower())


def verify_link_token(token):
    """Returns the email the token was issued for, or None if missing/invalid/expired."""
    if not token:
        return None
    try:
        return _serializer.loads(token, max_age=LINK_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def resolve_attendee_email(token_param):
    """A clicked magic link ('token_param') both authenticates this request AND makes it
    'sticky' — sets a persistent session cookie so future visits don't need the link kept
    or bookmarked. Falls back to that cookie when there's no token in the URL/request."""
    email = verify_link_token(token_param)
    if email:
        session['attendee_email'] = email
        session.permanent = True
        return email
    return session.get('attendee_email')


def make_staff_token(email):
    return _staff_serializer.dumps(email.lower())


def verify_staff_token(token):
    if not token:
        return None
    try:
        return _staff_serializer.loads(token, max_age=LINK_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def send_email(to_addr, subject, body, attachment=None, attachment_name=None,
                attachment_maintype='image', attachment_subtype='png'):
    if not SMTP_PASS:
        app.logger.warning('SMTP_APP_PASSWORD not set; skipping email to %s', to_addr)
        return
    try:
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = SMTP_FROM
        msg['To'] = to_addr
        msg.set_content(body)
        if attachment is not None:
            msg.add_attachment(attachment, maintype=attachment_maintype, subtype=attachment_subtype,
                                filename=attachment_name)
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(SMTP_FROM, SMTP_PASS)
            s.send_message(msg)
    except Exception:
        app.logger.exception('Failed to send email to %s', to_addr)


def send_bulk_pdfs(items, subject):
    """Like send_bulk_email but each recipient gets their own PDF attachment and body
    (e.g. each their own badge/certificate) — one SMTP connection reused for all of
    them. items: list of (to_addr, pdf_bytes, filename, body). Runs in a background
    thread so the admin's request returns immediately."""
    if not SMTP_PASS:
        app.logger.warning('SMTP_APP_PASSWORD not set; skipping bulk PDF email to %d recipients', len(items))
        return
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(SMTP_FROM, SMTP_PASS)
            for to_addr, pdf_bytes, filename, body in items:
                try:
                    msg = EmailMessage()
                    msg['Subject'] = subject
                    msg['From'] = SMTP_FROM
                    msg['To'] = to_addr
                    msg.set_content(body)
                    msg.add_attachment(pdf_bytes, maintype='application', subtype='pdf', filename=filename)
                    s.send_message(msg)
                except Exception:
                    app.logger.exception('Failed to send bulk PDF to %s', to_addr)
    except Exception:
        app.logger.exception('Bulk PDF email connection failed')


def send_bulk_email(recipients, subject, body):
    """One SMTP connection reused for every recipient, instead of opening/authenticating
    a fresh connection per person like send_email() does — matters once this is emailing
    hundreds of registrants rather than one confirmation at a time. Runs in a background
    thread from the caller so the admin's request returns immediately."""
    if not SMTP_PASS:
        app.logger.warning('SMTP_APP_PASSWORD not set; skipping bulk email to %d recipients', len(recipients))
        return
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
            s.login(SMTP_FROM, SMTP_PASS)
            for to_addr in recipients:
                try:
                    msg = EmailMessage()
                    msg['Subject'] = subject
                    msg['From'] = SMTP_FROM
                    msg['To'] = to_addr
                    msg.set_content(body)
                    s.send_message(msg)
                except Exception:
                    app.logger.exception('Failed to send bulk email to %s', to_addr)
    except Exception:
        app.logger.exception('Bulk email connection failed')


# Authoritative taxonomy per the ECRI 2026 Online Registration spec (the doc with the
# director copied in) — supersedes the earlier 4-theme placeholder set. Old abstracts/
# tracks/sessions are remapped onto these via THEME_MIGRATION_MAP below.
THEMES = [
    'Sexual and Reproductive Health (SRH)',
    'Maternal, Newborn and Child Health (MNCH)',
    'Key Populations and Community Health',
    'Climate, Environment and Health',
    'Digital Health, Data Science and Artificial Intelligence',
    'Implementation Science and Health Systems Research',
    'Operations, Research Management and Health Delivery Systems',
    'Social and Behavioural Sciences',
    'Health Economics, Financing and Sustainability',
    'Innovation, Entrepreneurship and Knowledge Translation',
    'Other',
]

THEME_DESCRIPTIONS = {
    'Sexual and Reproductive Health (SRH)': 'Sexual health, reproductive health services, HIV prevention, contraception, adolescent and youth health, and equitable access to care.',
    'Maternal, Newborn and Child Health (MNCH)': 'Maternal health, pregnancy outcomes, newborn care, child health, and life-course approaches.',
    'Key Populations and Community Health': 'Community-led approaches, differentiated services, rights-based programming, equity, and populations facing structural barriers to health services.',
    'Climate, Environment and Health': 'Climate-related health risks, environmental exposures, climate adaptation, health surveillance, resilience, and early warning systems.',
    'Digital Health, Data Science and Artificial Intelligence': 'Digital platforms, health information systems, artificial intelligence, data analytics, decision-support tools, and technology-enabled health solutions.',
    'Implementation Science and Health Systems Research': 'Translating evidence into practice, evaluating delivery models, improving programme effectiveness, and understanding adoption, sustainability and scale-up.',
    'Operations, Research Management and Health Delivery Systems': 'Finance, workforce, procurement, infrastructure, programme management, data systems, and operational processes that enable effective research and health programmes.',
    'Social and Behavioural Sciences': 'Social determinants of health, community engagement, behaviour change, equity, policy, and lived experiences.',
    'Health Economics, Financing and Sustainability': 'Costing, resource allocation, value for money, sustainable financing models, and evidence-informed investment decisions.',
    'Innovation, Entrepreneurship and Knowledge Translation': 'Development, evaluation, scaling, and application of new ideas, technologies, products, and approaches to improve health outcomes.',
    'Other': 'Anything not covered by the tracks above.',
}

THEME_STYLE = {
    'Sexual and Reproductive Health (SRH)':                        {'bg': '#F3EAF7', 'fg': '#6B3E8F', 'solid': '#8455A8'},
    'Maternal, Newborn and Child Health (MNCH)':                   {'bg': '#FBEAEF', 'fg': '#9C3A5C', 'solid': '#C15A7E'},
    'Key Populations and Community Health':                        {'bg': '#E6F5F3', 'fg': '#14766B', 'solid': '#1F9C8C'},
    'Climate, Environment and Health':                             {'bg': '#E6F3EC', 'fg': '#1F6B47', 'solid': '#2E8A5C'},
    'Digital Health, Data Science and Artificial Intelligence':    {'bg': '#E7F0FA', 'fg': '#0A5A9C', 'solid': '#1B79C9'},
    'Implementation Science and Health Systems Research':          {'bg': '#FBF0E4', 'fg': '#8A5A1E', 'solid': '#B5792A'},
    'Operations, Research Management and Health Delivery Systems': {'bg': '#EEF1F6', 'fg': '#3D4E68', 'solid': '#56698A'},
    'Social and Behavioural Sciences':                             {'bg': '#FBEEE8', 'fg': '#A14A2A', 'solid': '#C96A45'},
    'Health Economics, Financing and Sustainability':              {'bg': '#F7F3E3', 'fg': '#7A6A1E', 'solid': '#A38E2B'},
    'Innovation, Entrepreneurship and Knowledge Translation':      {'bg': '#EEEAFB', 'fg': '#4B3A8F', 'solid': '#6C54B5'},
    'Other':                                                       {'bg': '#F1F4F7', 'fg': '#68798F', 'solid': '#8496AB'},
    'Plenary':                                                     {'bg': '#E9ECF2', 'fg': '#37456B', 'solid': '#4A5B85'},
    'Logistics':                                                   {'bg': '#F1F4F7', 'fg': '#68798F', 'solid': '#8496AB'},
}

# Old 4-theme set -> new taxonomy, applied once at startup to any rows still on the
# old names (abstracts/sessions/sample data created before this migration).
THEME_MIGRATION_MAP = {
    'Data Science & Health Informatics': 'Digital Health, Data Science and Artificial Intelligence',
    'Sexual & Reproductive Health': 'Sexual and Reproductive Health (SRH)',
    'Climate, Environment & Health': 'Climate, Environment and Health',
    'Implementation Science': 'Implementation Science and Health Systems Research',
}

SESSION_THEMES = THEMES + ['Plenary', 'Logistics']

NEWS_TAG_STYLE = {
    'Announcement': {'bg': '#E7F0FA', 'fg': '#0A5A9C'},
    'Media': {'bg': '#F3EAF7', 'fg': '#6B3E8F'},
    'Community': {'bg': '#E6F3EC', 'fg': '#1F6B47'},
    'Award': {'bg': '#FBF0E4', 'fg': '#8A5A1E'},
}
NEWS_TAGS = list(NEWS_TAG_STYLE.keys())

FORMATS = ['Oral presentation', 'Poster', 'Either']

TITLES = ['Dr', 'Prof', 'Mr', 'Ms', 'Other']

PROFESSIONAL_CATEGORIES = [
    'Early Career Researcher', 'PhD Student', 'Masters Student', 'Undergraduate Student',
    'Research Scientist', 'Academic Staff', 'Healthcare Professional',
    'Government Representative', 'NGO/Development Partner', 'Private Sector', 'Other',
]

PARTICIPATION_CATEGORIES = [
    'Attendee', 'Oral Presenter', 'Poster Presenter', 'Innovation Booth Exhibitor',
    'Keynote Speaker', 'Panelist', 'Moderator/Chair', 'Sponsor/Partner',
    'Organising Committee', 'Media/Communications',
]
# Categories that trigger the conditional "Presentation Details" section
PRESENTING_CATEGORIES = ['Oral Presenter', 'Poster Presenter', 'Keynote Speaker', 'Panelist', 'Moderator/Chair']
# Categories that trigger the conditional "Poster & Innovation Booth" section
BOOTH_CATEGORIES = ['Poster Presenter', 'Innovation Booth Exhibitor']

# Zimbabwe first (host country), then the rest of the SADC region, then everywhere
# else alphabetically — most registrants will find their country in the first screen.
COUNTRIES = [
    'Zimbabwe',
    'Angola', 'Botswana', 'Democratic Republic of Congo', 'Eswatini', 'Lesotho',
    'Malawi', 'Mauritius', 'Mozambique', 'Namibia', 'Seychelles', 'South Africa',
    'Tanzania', 'Zambia',
    'Afghanistan', 'Albania', 'Algeria', 'Argentina', 'Australia', 'Austria',
    'Bangladesh', 'Belgium', 'Benin', 'Brazil', 'Burkina Faso', 'Burundi',
    'Cameroon', 'Canada', 'Chad', 'Chile', 'China', 'Colombia', 'Cote d\'Ivoire',
    'Denmark', 'Egypt', 'Ethiopia', 'Finland', 'France', 'Gabon', 'Gambia',
    'Germany', 'Ghana', 'Greece', 'Guinea', 'India', 'Indonesia', 'Ireland',
    'Israel', 'Italy', 'Japan', 'Kenya', 'Liberia', 'Libya',
    'Madagascar', 'Mali', 'Mauritania', 'Mexico', 'Morocco', 'Nepal',
    'Netherlands', 'New Zealand', 'Niger', 'Nigeria', 'Norway', 'Pakistan',
    'Philippines', 'Poland', 'Portugal', 'Rwanda', 'Saudi Arabia', 'Senegal',
    'Sierra Leone', 'Singapore', 'Somalia', 'South Korea', 'South Sudan', 'Spain',
    'Sri Lanka', 'Sudan', 'Sweden', 'Switzerland', 'Thailand', 'Togo', 'Tunisia',
    'Turkey', 'Uganda', 'Ukraine', 'United Arab Emirates', 'United Kingdom',
    'United States', 'Vietnam', 'Other',
]

PRESENTATION_TYPES = ['Oral Presentation', 'Poster Presentation', 'Innovation Pitch']
ATTENDANCE_MODES = ['Physical', 'Virtual', 'Hybrid']
DAYS_OPTIONS = ['Day 1', 'Day 2']
EQUIPMENT_OPTIONS = ['Table', 'Power supply', 'Screen', 'Internet access', 'Other']

ABSTRACT_WORD_LIMIT = 300
# The abstract stays a single free-text field, not split into Background/Methods/
# Results/Conclusion sub-fields — the client's call: authors should be free to weight
# a longer Methods section against a shorter Results one as the work needs, not be
# boxed into a fixed per-section split of the same 300 words.
TITLE_WORD_LIMIT = 20

CAREER_STAGES = [
    'Undergraduate student', 'Masters student', 'PhD student',
    'Postdoctoral researcher', 'Research fellow', 'Early career professional',
]
SUBMISSION_DEADLINE = 'Friday 2 October 2026, 23:59 CAT'
EVENT_CAPACITY = 400

# Server timezone is Africa/Harare (CAT), same as the event itself, so plain local
# datetimes below need no timezone conversion.
EVENT_DATES = {1: datetime.date(2026, 10, 22), 2: datetime.date(2026, 10, 23)}
REMINDER_MINUTES_BEFORE = 15

# Per the ECRI Abstract Submission Module spec's "Scientific Review Dashboard" section
# — replaces the earlier placeholder 4-criteria set (Originality/Methods/Relevance/
# Clarity). Referenced dynamically everywhere (scoring insert/update, mean-score SQL,
# scoring_sheet.html) via CRITERIA_KEYS, so changing this list is a one-place edit.
CRITERIA = [
    {'key': 'sci_quality', 'label': 'Scientific quality', 'hint': 'Soundness of the science — design, analysis, interpretation.'},
    {'key': 'innovation', 'label': 'Innovation / originality', 'hint': 'Does the work add something not already established?'},
    {'key': 'relevance', 'label': 'Relevance', 'hint': 'Significance for health, policy or practice in Zimbabwe.'},
    {'key': 'method_quality', 'label': 'Methodological quality', 'hint': 'Appropriateness and rigour of the methods used.'},
    {'key': 'presentation', 'label': 'Presentation readiness', 'hint': 'Is the abstract clear, well structured and ready to present?'},
]
CRITERIA_KEYS = [c['key'] for c in CRITERIA]


def mean_score_sql():
    """SQL expression for a review's mean score across the current CRITERIA columns —
    used wherever the DB needs to average a row's scores, kept in one place so the
    criteria list can change without hunting down every query."""
    return '(' + '+'.join(CRITERIA_KEYS) + f') / {len(CRITERIA_KEYS)}.0'

RECOMMENDATIONS = ['Accept as oral', 'Accept as poster', 'Revise and resubmit', 'Reject']

RECOMMENDATION_TO_STATUS = {
    'Accept as oral': 'Accepted — oral',
    'Accept as poster': 'Accepted — poster',
    'Revise and resubmit': 'Revise and resubmit',
    'Reject': 'Not accepted',
}

# Allocation slots (day/time/room) live in the allocation_slots table, not a fixed
# list — chairs can add slots at any time, not just a preset grid. See init_db() for
# the default 15-minute 09:00-16:30 grid seeded once for each day.

DECISION_STATUSES = ['Under review', 'Accepted — oral', 'Accepted — poster', 'Revise and resubmit', 'Not accepted']

# 'Not required' unless an admin turns payment on; 'Pending' from registration until
# a reference/screenshot is submitted; 'Submitted' awaits admin review of that proof.
PAYMENT_STATUSES = ['Not required', 'Pending', 'Submitted', 'Paid', 'Rejected']

ALLOWED_DOC_EXT = ('.pdf', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx')
MAX_DOC_BYTES = 15 * 1024 * 1024

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
PHOTO_PATH_RE = re.compile(r'^uploads/[0-9a-f]{24}\.jpg$')
DOC_PATH_RE = re.compile(r'^uploads/docs/[0-9a-f]{24}\.[a-zA-Z0-9]{2,5}$')

app = Flask(__name__, static_url_path='/ecri/static')

# On ecri.online addresses carry no /ecri prefix (/register, not /ecri/register). Internally the app
# keeps its /ecri/... routes: a small middleware maps the clean path in, and pub_path() maps it back out.
# Old /ecri/... addresses keep working on every host.
CLEAN_HOSTS = {'ecri.online', 'www.ecri.online'}


def is_clean_host():
    return has_request_context() and request.host.split(':')[0].lower() in CLEAN_HOSTS


def pub_path(path):
    """Public form of an internal /ecri/... path: bare on the new domain, unchanged elsewhere."""
    if isinstance(path, str) and is_clean_host():
        if path == '/ecri':
            return '/'
        if path.startswith('/ecri/') or path.startswith('/ecri?'):
            return path[5:] or '/'
    return path


class CleanHostMiddleware:
    def __init__(self, wsgi):
        self.wsgi = wsgi

    def __call__(self, environ, start_response):
        host = environ.get('HTTP_HOST', '').split(':')[0].lower()
        p = environ.get('PATH_INFO', '/')
        if host in CLEAN_HOSTS and not (p == '/ecri' or p.startswith('/ecri/')):
            environ['PATH_INFO'] = '/ecri' + p
        return self.wsgi(environ, start_response)


app.wsgi_app = CleanHostMiddleware(app.wsgi_app)
app.secret_key = SECRET_KEY
app.permanent_session_lifetime = datetime.timedelta(days=45)  # covers the whole event window


@app.context_processor
def inject_nav_flags():
    # Research in focus / Award / Downloads stay out of nav and the Home explore
    # grid until an admin has actually put something in them — an empty page
    # linked from the nav reads as an unfinished site, not a feature in progress.
    db = get_db()
    has_award = db.execute(
        "SELECT 1 FROM award_page WHERE id = 'main' AND (COALESCE(headline, '') != '' OR COALESCE(body, '') != '')"
    ).fetchone()
    staff = current_staff()
    return {
        'has_research_focus': bool(db.execute("SELECT 1 FROM research_focus LIMIT 1").fetchone()),
        'has_downloads': bool(db.execute("SELECT 1 FROM downloads LIMIT 1").fetchone()),
        'has_award': bool(has_award),
        'my_staff_roles': (staff['roles'] or '').split(',') if staff else [],
    }


def current_staff():
    email = session.get('staff_email')
    if not email:
        return None
    db = get_db()
    row = db.execute("SELECT * FROM staff_users WHERE email = ?", (email,)).fetchone()
    return row


def staff_can_access_theme(staff, theme):
    roles = (staff['roles'] or '').split(',')
    if 'admin' in roles or 'chair' in roles:
        return True
    tracks = [t for t in (staff['tracks'] or '').split('|') if t]
    return theme in tracks


def assigned_reviewer_count(db, theme):
    """How many reviewer accounts are actually assigned to this track — the number of
    reviews needed before a decision counts as 'complete', whatever that number is
    (could legitimately be 1)."""
    count = 0
    for row in db.execute("SELECT tracks FROM staff_users WHERE roles LIKE '%reviewer%'"):
        tracks = [t for t in (row['tracks'] or '').split('|') if t]
        if theme in tracks:
            count += 1
    return count


def require_role(role):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            staff = current_staff()
            if not staff or role not in (staff['roles'] or '').split(','):
                return redirect(url_for('staff_login_page', next=pub_path(request.path)))
            return fn(*args, **kwargs)
        return wrapped
    return decorator


@app.after_request
def no_cache(response):
    # Pages here reflect live DB state (submissions, tokens, statuses) and must
    # never be served stale from disk cache or the back/forward cache.
    if not request.path.startswith('/ecri/static/'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return response


@app.after_request
def clean_redirect_location(response):
    loc = response.headers.get('Location')
    if loc and is_clean_host():
        if loc.startswith('http'):
            # Werkzeug's own redirects (e.g. adding a trailing slash) come back absolute: tidy path and scheme
            u = urlsplit(loc)
            if u.netloc.split(':')[0].lower() in CLEAN_HOSTS:
                loc = urlunsplit(('https', u.netloc.split(':')[0], pub_path(u.path), u.query, u.fragment))
        else:
            loc = pub_path(loc)
        response.headers['Location'] = loc
    return response


@app.context_processor
def inject_prefix():
    # b: the path prefix templates put in front of internal addresses ('' on the new domain)
    return {'b': '' if is_clean_host() else '/ecri', 'public_base': PUBLIC_BASE}


_jinja_url_for = app.jinja_env.globals['url_for']
app.jinja_env.globals['url_for'] = lambda *a, **k: pub_path(_jinja_url_for(*a, **k))


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys = ON')
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS registrations (
            id            TEXT PRIMARY KEY,
            code          TEXT UNIQUE NOT NULL,
            name          TEXT NOT NULL,
            email         TEXT NOT NULL,
            institution   TEXT NOT NULL,
            role          TEXT NOT NULL,
            attendance    TEXT NOT NULL,
            created_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS abstracts (
            ref                 TEXT PRIMARY KEY,
            title               TEXT NOT NULL,
            theme               TEXT NOT NULL,
            format              TEXT NOT NULL,
            body                TEXT NOT NULL,
            word_count          INTEGER NOT NULL,
            ethics_ref          TEXT,
            presenting_author   TEXT NOT NULL,
            institution         TEXT,
            email               TEXT NOT NULL,
            co_authors          TEXT,
            status              TEXT NOT NULL DEFAULT 'Under review',
            created_at          TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS abstract_authors (
            id            TEXT PRIMARY KEY,
            abstract_ref  TEXT NOT NULL,
            name          TEXT NOT NULL,
            institution   TEXT,
            country       TEXT,
            role          TEXT,
            position      INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS counters (
            name  TEXT PRIMARY KEY,
            value INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS staff_users (
            id             TEXT PRIMARY KEY,
            email          TEXT UNIQUE NOT NULL,
            password_hash  TEXT NOT NULL,
            name           TEXT NOT NULL,
            roles          TEXT NOT NULL,
            created_at     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reviews (
            id               TEXT PRIMARY KEY,
            abstract_ref     TEXT NOT NULL,
            reviewer_email   TEXT NOT NULL,
            orig             INTEGER,
            meth             INTEGER,
            rel              INTEGER,
            clar             INTEGER,
            comment          TEXT,
            recommendation   TEXT,
            submitted_at     TEXT NOT NULL,
            UNIQUE(abstract_ref, reviewer_email)
        );

        CREATE TABLE IF NOT EXISTS allocations (
            slot_id       TEXT PRIMARY KEY,
            abstract_ref  TEXT NOT NULL,
            allocated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS announcements (
            id         TEXT PRIMARY KEY,
            headline   TEXT NOT NULL,
            body       TEXT,
            priority   TEXT NOT NULL,
            sent_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS news_items (
            id           TEXT PRIMARY KEY,
            tag          TEXT,
            headline     TEXT NOT NULL,
            body         TEXT,
            image        TEXT,
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS gallery_images (
            id           TEXT PRIMARY KEY,
            path         TEXT NOT NULL,
            caption      TEXT,
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS research_focus (
            abstract_ref TEXT PRIMARY KEY,
            headline     TEXT,
            q_about      TEXT,
            q_problem    TEXT,
            q_matters    TEXT,
            q_hoping     TEXT,
            credit       TEXT,
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS engage_posts (
            id           TEXT PRIMARY KEY,
            email        TEXT NOT NULL,
            author_name  TEXT NOT NULL,
            body         TEXT NOT NULL,
            approved     INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS engage_replies (
            id           TEXT PRIMARY KEY,
            post_id      TEXT NOT NULL,
            email        TEXT NOT NULL,
            author_name  TEXT NOT NULL,
            body         TEXT NOT NULL,
            approved     INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS abstract_notes (
            id             TEXT PRIMARY KEY,
            abstract_ref   TEXT NOT NULL,
            author_email   TEXT NOT NULL,
            author_name    TEXT NOT NULL,
            body           TEXT NOT NULL,
            created_at     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id             TEXT PRIMARY KEY,
            day            INTEGER NOT NULL,
            time           TEXT NOT NULL,
            title          TEXT NOT NULL,
            who            TEXT,
            room           TEXT,
            theme          TEXT NOT NULL,
            abstract_ref   TEXT,
            created_at     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS session_stars (
            email        TEXT NOT NULL,
            session_id   TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            reminded_at  TEXT,
            PRIMARY KEY (email, session_id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key    TEXT PRIMARY KEY,
            value  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS downloads (
            id            TEXT PRIMARY KEY,
            title         TEXT NOT NULL,
            description   TEXT,
            file_path     TEXT NOT NULL,
            original_name TEXT,
            created_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS award_page (
            id          TEXT PRIMARY KEY,
            headline    TEXT,
            body        TEXT,
            image       TEXT,
            updated_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS allocation_slots (
            id           TEXT PRIMARY KEY,
            day          INTEGER NOT NULL,
            time         TEXT NOT NULL,
            room         TEXT NOT NULL DEFAULT '',
            created_at   TEXT NOT NULL
        );
        """
    )
    db.execute("INSERT OR IGNORE INTO counters (name, value) VALUES ('abstract_ref', 0)")
    db.execute("INSERT OR IGNORE INTO counters (name, value) VALUES ('registration_code', 300)")

    try:
        db.execute("ALTER TABLE staff_users ADD COLUMN tracks TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # column already exists

    try:
        db.execute("ALTER TABLE abstracts ADD COLUMN bio TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # column already exists

    try:
        db.execute("ALTER TABLE abstracts ADD COLUMN photo TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # column already exists

    try:
        db.execute("ALTER TABLE session_stars ADD COLUMN reminded_at TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    try:
        db.execute("ALTER TABLE registrations ADD COLUMN payment_status TEXT NOT NULL DEFAULT 'Not required'")
    except sqlite3.OperationalError:
        pass  # column already exists

    try:
        db.execute("ALTER TABLE registrations ADD COLUMN payment_reference TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # column already exists

    try:
        db.execute("ALTER TABLE registrations ADD COLUMN payment_proof TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Registration form overhaul (per the ECRI 2026 Online Registration spec) — all
    # additive, so nothing already recorded is disturbed. 'attendance' is repurposed
    # in place to hold Physical/Virtual/Hybrid instead of the old In person/Online
    # values, since it's the same concept just with an added Hybrid option.
    for col, default in [
        ('title', ''), ('first_name', ''), ('surname', ''), ('preferred_name', ''),
        ('gender', ''), ('phone', ''), ('country', ''), ('city', ''),
        ('department', ''), ('job_title', ''), ('professional_category', ''),
        ('participation_category', ''), ('research_interests', ''),
        ('presentation_type', ''), ('presentation_title', ''), ('abstract_id_ref', ''), ('supervisor_name', ''),
        ('poster_printing_support', ''), ('innovation_title', ''), ('innovation_description', ''),
        ('equipment_required', ''), ('days_attending', ''), ('cocktail_attendance', ''),
        ('dietary_requirements', ''), ('accessibility_requirements', ''),
        ('travelling_outside_harare', ''), ('travel_origin', ''),
        ('transport_requirements', ''), ('accommodation_requirements', ''),
        ('consent_photo', ''), ('consent_comms', ''),
        ('checked_in_at', None), ('certificate_issued_at', None),
    ]:
        try:
            if default is None:
                db.execute(f"ALTER TABLE registrations ADD COLUMN {col} TEXT")
            else:
                db.execute(f"ALTER TABLE registrations ADD COLUMN {col} TEXT NOT NULL DEFAULT '{default}'")
        except sqlite3.OperationalError:
            pass  # column already exists

    try:
        db.execute("ALTER TABLE abstracts ADD COLUMN country TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # column already exists

    try:
        db.execute("ALTER TABLE abstracts ADD COLUMN career_stage TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # column already exists

    # New 5-criteria review scoring (Scientific quality / Innovation / Relevance /
    # Methodological quality / Presentation readiness), additive — the old orig/meth/
    # clar columns stay in place unused rather than migrating the handful of reviews
    # already scored under the old 4-criteria system.
    for col in CRITERIA_KEYS:
        try:
            db.execute(f"ALTER TABLE reviews ADD COLUMN {col} INTEGER")
        except sqlite3.OperationalError:
            pass  # column already exists

    # Backfill first_name/surname for anyone registered before the split — best-effort
    # so old rows aren't left blank; 'name' stays the source of truth either way.
    for row in db.execute("SELECT id, name FROM registrations WHERE first_name = ''").fetchall():
        parts = (row[1] or '').strip().split(' ', 1)
        first = parts[0] if parts else ''
        last = parts[1] if len(parts) > 1 else ''
        db.execute("UPDATE registrations SET first_name = ?, surname = ? WHERE id = ?", (first, last, row[0]))

    # One-time remap onto the new 11-theme taxonomy — a no-op once no rows carry the
    # old names any more, so safe to run on every startup.
    for old, new in THEME_MIGRATION_MAP.items():
        db.execute("UPDATE abstracts SET theme = ? WHERE theme = ?", (new, old))
        db.execute("UPDATE sessions SET theme = ? WHERE theme = ?", (new, old))
        for row in db.execute("SELECT id, tracks FROM staff_users WHERE tracks LIKE ?", (f'%{old}%',)).fetchall():
            tracks = [new if t == old else t for t in row[1].split('|')]
            db.execute("UPDATE staff_users SET tracks = ? WHERE id = ?", ('|'.join(tracks), row[0]))

    # Seed a default 15-minute allocation grid (09:00-16:30, both days) once, only if
    # no slots exist yet — chairs can add/remove slots freely from there, including at
    # odd times outside this grid, so this is just a convenient starting point.
    if not db.execute("SELECT 1 FROM allocation_slots LIMIT 1").fetchone():
        start_minutes, end_minutes = 9 * 60, 16 * 60 + 30
        for day in (1, 2):
            for minutes in range(start_minutes, end_minutes, 15):
                time_str = f'{minutes // 60:02d}:{minutes % 60:02d}'
                db.execute(
                    "INSERT INTO allocation_slots (id, day, time, room, created_at) VALUES (?, ?, ?, '', ?)",
                    (os.urandom(8).hex(), day, time_str, datetime.datetime.utcnow().isoformat()),
                )

    os.makedirs(os.path.join(BASE_DIR, 'static', 'uploads'), exist_ok=True)
    os.makedirs(os.path.join(BASE_DIR, 'static', 'uploads', 'docs'), exist_ok=True)

    db.commit()
    db.close()


def get_setting(db, key, default=''):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row['value'] if row else default


def set_setting(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def next_abstract_ref(db):
    """Derived from existing rows rather than a separate counter, so it can't drift out of sync."""
    row = db.execute(
        "SELECT MAX(CAST(SUBSTR(ref, 6) AS INTEGER)) AS n FROM abstracts"
    ).fetchone()
    n = (row['n'] or 0) + 1
    return f'ECRI-{n:03d}'


def next_registration_code(db):
    row = db.execute(
        "SELECT MAX(CAST(SUBSTR(code, 9) AS INTEGER)) AS n FROM registrations"
    ).fetchone()
    n = max(row['n'] or 0, 300) + 1
    return f'ECRI-26-{n:04d}'


def word_count(text):
    words = text.strip().split()
    return len(words)


def normalize_session_time(time_str):
    """Zero-pad the hour so 'time' sorts correctly as a plain string (e.g. '9:15' -> '09:15'
    — otherwise '15:00' sorts before '9:15' alphabetically, since '1' < '9')."""
    m = re.match(r'^\s*(\d{1,2}):(\d{2})\s*$', time_str)
    if m:
        hour, minute = m.groups()
        return f'{int(hour):02d}:{minute}'
    return time_str.strip()


@app.route('/ecri/')
def home():
    db = get_db()
    announcements = db.execute(
        "SELECT * FROM announcements ORDER BY sent_at DESC LIMIT 10"
    ).fetchall()
    accepted_count = db.execute(
        "SELECT COUNT(*) c FROM abstracts WHERE status IN ('Accepted — oral', 'Accepted — poster')"
    ).fetchone()['c']
    return render_template(
        'home.html',
        deadline=SUBMISSION_DEADLINE,
        announcements=announcements,
        theme_count=len(THEMES),
        accepted_count=accepted_count,
    )


@app.route('/ecri/register', methods=['GET'])
def register_page():
    return render_template(
        'register.html',
        titles=TITLES,
        professional_categories=PROFESSIONAL_CATEGORIES,
        participation_categories=PARTICIPATION_CATEGORIES,
        presenting_categories=PRESENTING_CATEGORIES,
        booth_categories=BOOTH_CATEGORIES,
        presentation_types=PRESENTATION_TYPES,
        attendance_modes=ATTENDANCE_MODES,
        days_options=DAYS_OPTIONS,
        equipment_options=EQUIPMENT_OPTIONS,
        themes=THEMES,
        countries=COUNTRIES,
    )


@app.route('/ecri/api/register', methods=['POST'])
def api_register():
    data = request.json or {}

    def field(key):
        return (data.get(key) or '').strip()

    title = field('title')
    first_name = field('first_name')
    surname = field('surname')
    preferred_name = field('preferred_name')
    gender = field('gender')
    email = field('email')
    phone = field('phone')
    country = field('country')
    city = field('city')
    institution = field('institution')
    department = field('department')
    job_title = field('job_title')
    professional_category = field('professional_category')
    participation_category = field('participation_category')
    research_interests = [t for t in (data.get('research_interests') or []) if t in THEMES]
    presentation_type = field('presentation_type')
    presentation_title = field('presentation_title')
    abstract_id_ref = field('abstract_id_ref')
    supervisor_name = field('supervisor_name')
    poster_printing_support = field('poster_printing_support')
    innovation_title = field('innovation_title')
    innovation_description = field('innovation_description')
    equipment_required = [e for e in (data.get('equipment_required') or []) if e in EQUIPMENT_OPTIONS]
    attendance_mode = field('attendance_mode')
    days_attending = [d for d in (data.get('days_attending') or []) if d in DAYS_OPTIONS]
    cocktail_attendance = field('cocktail_attendance')
    dietary_requirements = field('dietary_requirements')
    accessibility_requirements = field('accessibility_requirements')
    travelling_outside_harare = field('travelling_outside_harare')
    travel_origin = field('travel_origin')
    transport_requirements = field('transport_requirements')
    accommodation_requirements = field('accommodation_requirements')
    consent_photo = 'Yes' if data.get('consent_photo') else 'No'
    consent_comms = 'Yes' if data.get('consent_comms') else 'No'

    name = f'{first_name} {surname}'.strip()
    is_presenter = participation_category in PRESENTING_CATEGORIES
    is_booth = participation_category in BOOTH_CATEGORIES

    errors = {}
    if not first_name:
        errors['first_name'] = 'First name is required.'
    if not surname:
        errors['surname'] = 'Surname is required.'
    if not email:
        errors['email'] = 'Email is required.'
    elif not EMAIL_RE.match(email):
        errors['email'] = 'Enter a valid email address.'
    if not country:
        errors['country'] = 'Country is required.'
    if not institution:
        errors['institution'] = 'Organisation / institution is required.'
    if professional_category not in PROFESSIONAL_CATEGORIES:
        errors['professional_category'] = 'Choose a professional category.'
    if participation_category not in PARTICIPATION_CATEGORIES:
        errors['participation_category'] = 'Choose a participation category.'
    if attendance_mode not in ATTENDANCE_MODES:
        errors['attendance_mode'] = 'Choose an attendance mode.'
    if not days_attending:
        errors['days_attending'] = 'Choose at least one day.'
    if errors:
        return jsonify({'ok': False, 'errors': errors}), 400

    db = get_db()
    payment_required = get_setting(db, 'payment_required', '0') == '1'
    payment_amount = get_setting(db, 'payment_amount', '')
    payment_instructions = get_setting(db, 'payment_instructions', '')

    # One registration per email, permanently — re-submitting (double-click, resent
    # form, or registering again later) returns the existing registration instead
    # of creating a duplicate.
    existing = db.execute(
        "SELECT * FROM registrations WHERE email = ? ORDER BY created_at DESC LIMIT 1",
        (email,),
    ).fetchone()
    if existing:
        code = existing['code']
        qr_img = qrcode.make(code)
        buf = io.BytesIO()
        qr_img.save(buf, format='PNG')
        qr_b64 = base64.b64encode(buf.getvalue()).decode('ascii')
        return jsonify({
            'ok': True, 'code': code, 'email': email, 'qr': qr_b64, 'already_registered': True,
            'payment_required': payment_required, 'payment_status': existing['payment_status'],
            'payment_amount': payment_amount, 'payment_instructions': payment_instructions,
            'link_token': make_link_token(email),
        })

    code = next_registration_code(db)
    reg_id = os.urandom(8).hex()
    initial_payment_status = 'Pending' if payment_required else 'Not required'
    db.execute(
        "INSERT INTO registrations (id, code, name, email, institution, role, attendance, payment_status, created_at, "
        "title, first_name, surname, preferred_name, gender, phone, country, city, department, job_title, "
        "professional_category, participation_category, research_interests, presentation_type, presentation_title, "
        "abstract_id_ref, supervisor_name, poster_printing_support, innovation_title, innovation_description, "
        "equipment_required, days_attending, cocktail_attendance, dietary_requirements, accessibility_requirements, "
        "travelling_outside_harare, travel_origin, transport_requirements, accommodation_requirements, "
        "consent_photo, consent_comms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (reg_id, code, name, email, institution, professional_category, attendance_mode, initial_payment_status,
         datetime.datetime.utcnow().isoformat(),
         title, first_name, surname, preferred_name, gender, phone, country, city, department, job_title,
         professional_category, participation_category, '|'.join(research_interests), presentation_type,
         presentation_title, abstract_id_ref, supervisor_name, poster_printing_support, innovation_title,
         innovation_description, '|'.join(equipment_required), '|'.join(days_attending), cocktail_attendance,
         dietary_requirements, accessibility_requirements, travelling_outside_harare, travel_origin,
         transport_requirements, accommodation_requirements, consent_photo, consent_comms),
    )
    db.commit()

    qr_img = qrcode.make(code)
    buf = io.BytesIO()
    qr_img.save(buf, format='PNG')
    qr_bytes = buf.getvalue()
    qr_b64 = base64.b64encode(qr_bytes).decode('ascii')

    payment_outstanding = payment_required and initial_payment_status != 'Paid'
    if payment_outstanding:
        subject = 'ECRI 2026 — registration received, payment pending'
        intro = (
            f'Your place is reserved for ECRI 2026 (22-23 October, Harare) but your '
            f'registration is only finalised once your payment is verified.\n\n'
            f'Registration fee: {payment_amount}\n{payment_instructions}\n\n'
            f'Once paid, submit your reference or a screenshot from My schedule:\n'
            f'{PUBLIC_BASE}/my-schedule?token={make_link_token(email)}\n'
        )
    else:
        subject = 'ECRI 2026 — registration confirmed'
        intro = f'You are registered for ECRI 2026 (22-23 October, Harare).\n'
    send_email(
        email,
        subject,
        f'Hi {name},\n\n'
        f'{intro}'
        f'Attendance: {attendance_mode} · {", ".join(days_attending)}\n'
        f'Your registration code: {code}\n\n'
        f'Your QR code is attached.\n\n'
        f'CeSHHAR Zimbabwe · ECRI 2026',
        attachment=qr_bytes,
        attachment_name=f'{code}.png',
    )

    return jsonify({
        'ok': True, 'code': code, 'email': email, 'qr': qr_b64,
        'payment_required': payment_required, 'payment_status': initial_payment_status,
        'payment_amount': payment_amount, 'payment_instructions': payment_instructions,
        'link_token': make_link_token(email),
    })


MAX_PHOTO_BYTES = 5 * 1024 * 1024  # 5MB
PHOTO_MAX_DIM = 800  # px, longest side


@app.route('/ecri/api/upload-photo', methods=['POST'])
def api_upload_photo():
    f = request.files.get('photo')
    if not f or not f.filename:
        return jsonify({'ok': False, 'error': 'No file received.'}), 400

    raw = f.read(MAX_PHOTO_BYTES + 1)
    if len(raw) > MAX_PHOTO_BYTES:
        return jsonify({'ok': False, 'error': 'Image must be under 5MB.'}), 400

    try:
        img = Image.open(io.BytesIO(raw))
        img.verify()
        img = Image.open(io.BytesIO(raw))  # re-open: verify() consumes the parser
    except (UnidentifiedImageError, OSError):
        return jsonify({'ok': False, 'error': 'That does not look like a valid image file.'}), 400

    img = img.convert('RGB')
    img.thumbnail((PHOTO_MAX_DIM, PHOTO_MAX_DIM))

    filename = f'{os.urandom(12).hex()}.jpg'
    path = os.path.join(BASE_DIR, 'static', 'uploads', filename)
    img.save(path, format='JPEG', quality=85)

    return jsonify({'ok': True, 'photo': f'uploads/{filename}'})


def save_abstract_authors(db, ref, authors_data):
    """Full replace, not merge — simplest way to support add/remove/reorder from a
    client that just resubmits its current list each time."""
    db.execute("DELETE FROM abstract_authors WHERE abstract_ref = ?", (ref,))
    for i, a in enumerate(authors_data or []):
        name = (a.get('name') or '').strip()
        if not name:
            continue
        db.execute(
            "INSERT INTO abstract_authors (id, abstract_ref, name, institution, country, role, position, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (os.urandom(8).hex(), ref, name, (a.get('institution') or '').strip(),
             (a.get('country') or '').strip(), (a.get('role') or '').strip(), i,
             datetime.datetime.utcnow().isoformat()),
        )


def get_abstract_authors(db, ref):
    rows = db.execute(
        "SELECT name, institution, country, role FROM abstract_authors WHERE abstract_ref = ? ORDER BY position", (ref,)
    ).fetchall()
    return [dict(r) for r in rows]


def split_name(full_name):
    """Best-effort split of a stored full name into (first_name, surname)."""
    parts = (full_name or '').strip().split()
    if not parts:
        return '', ''
    if len(parts) == 1:
        return parts[0], ''
    return ' '.join(parts[:-1]), parts[-1]


def validate_abstract_fields(data):
    """Shared by submit + edit. Returns (values_dict, errors_dict)."""
    title = (data.get('title') or '').strip()
    theme = (data.get('theme') or '').strip()
    fmt = (data.get('format') or '').strip()
    body = (data.get('body') or '').strip()
    ethics_ref = (data.get('ethics_ref') or '').strip()
    first_name = (data.get('first_name') or '').strip()
    surname = (data.get('surname') or '').strip()
    presenting_author = f'{first_name} {surname}'.strip()
    institution = (data.get('institution') or '').strip()
    email = (data.get('email') or '').strip()
    country = (data.get('country') or '').strip()
    career_stage = (data.get('career_stage') or '').strip()
    bio = (data.get('bio') or '').strip()
    photo = (data.get('photo') or '').strip()
    declared = bool(data.get('declared'))
    authors_data = data.get('authors') or []

    title_wc = word_count(title)
    wc = word_count(body)

    errors = {}
    if not title:
        errors['title'] = 'Abstract title is required.'
    elif title_wc > TITLE_WORD_LIMIT:
        errors['title'] = f'Title is {title_wc} words; the limit is {TITLE_WORD_LIMIT}.'
    if theme not in THEMES:
        errors['theme'] = 'Choose a theme.'
    if fmt not in FORMATS:
        errors['format'] = 'Choose a preferred format.'
    if not body:
        errors['body'] = 'The abstract body is required.'
    elif wc > ABSTRACT_WORD_LIMIT:
        errors['body'] = f'Abstract is {wc} words; the limit is {ABSTRACT_WORD_LIMIT}.'
    if not first_name:
        errors['first_name'] = 'First name is required.'
    if not surname:
        errors['surname'] = 'Surname is required.'
    if not email:
        errors['email'] = 'Email is required.'
    elif not EMAIL_RE.match(email):
        errors['email'] = 'Enter a valid email address.'
    if not declared:
        errors['declared'] = 'You must confirm the declaration.'
    if photo and (not PHOTO_PATH_RE.match(photo) or not os.path.exists(os.path.join(BASE_DIR, 'static', photo))):
        errors['photo'] = 'Photo upload did not complete — please re-upload.'

    values = {
        'title': title, 'theme': theme, 'format': fmt, 'body': body, 'word_count': wc,
        'ethics_ref': ethics_ref, 'presenting_author': presenting_author, 'institution': institution,
        'email': email, 'country': country, 'career_stage': career_stage, 'bio': bio, 'photo': photo,
        'authors_data': authors_data,
    }
    return values, errors


@app.route('/ecri/submit', methods=['GET'])
def submit_page():
    return render_template(
        'submit.html',
        themes=THEMES,
        theme_style=THEME_STYLE,
        formats=FORMATS,
        word_limit=ABSTRACT_WORD_LIMIT,
        title_word_limit=TITLE_WORD_LIMIT,
        career_stages=CAREER_STAGES,
        countries=COUNTRIES,
        deadline=SUBMISSION_DEADLINE,
        edit_ref=None,
        edit_token=None,
        prefill=None,
        prefill_first_name='',
        prefill_surname='',
        authors_prefill=[],
    )


@app.route('/ecri/edit/<ref>', methods=['GET'])
def edit_page(ref):
    email = resolve_attendee_email(request.args.get('token'))
    db = get_db()
    row = db.execute("SELECT * FROM abstracts WHERE ref = ?", (ref,)).fetchone()
    if not row or not email or row['email'].lower() != email or row['status'] != 'Under review':
        return render_template('edit_denied.html'), 403
    first_name, surname = split_name(row['presenting_author'])
    return render_template(
        'submit.html',
        themes=THEMES,
        theme_style=THEME_STYLE,
        formats=FORMATS,
        word_limit=ABSTRACT_WORD_LIMIT,
        title_word_limit=TITLE_WORD_LIMIT,
        career_stages=CAREER_STAGES,
        countries=COUNTRIES,
        deadline=SUBMISSION_DEADLINE,
        edit_ref=ref,
        edit_token=request.args.get('token'),
        prefill=row,
        prefill_first_name=first_name,
        prefill_surname=surname,
        authors_prefill=get_abstract_authors(db, ref),
    )


@app.route('/ecri/api/edit/<ref>', methods=['POST'])
def api_edit(ref):
    data = request.json or {}
    email = resolve_attendee_email(data.get('_token'))

    db = get_db()
    row = db.execute("SELECT * FROM abstracts WHERE ref = ?", (ref,)).fetchone()
    if not row or not email or row['email'].lower() != email or row['status'] != 'Under review':
        return jsonify({'ok': False, 'errors': {'title': 'This link has expired or this submission can no longer be edited. Request a new link from My submissions.'}}), 403

    v, errors = validate_abstract_fields(data)
    if errors:
        return jsonify({'ok': False, 'errors': errors}), 400

    db.execute(
        "UPDATE abstracts SET title=?, theme=?, format=?, body=?, word_count=?, ethics_ref=?, "
        "presenting_author=?, institution=?, email=?, country=?, career_stage=?, bio=?, photo=? WHERE ref=?",
        (v['title'], v['theme'], v['format'], v['body'], v['word_count'], v['ethics_ref'],
         v['presenting_author'], v['institution'], v['email'], v['country'], v['career_stage'],
         v['bio'], v['photo'], ref),
    )
    save_abstract_authors(db, ref, v['authors_data'])
    db.commit()

    return jsonify({'ok': True, 'ref': ref})


@app.route('/ecri/api/submit', methods=['POST'])
def api_submit():
    data = request.json or {}
    v, errors = validate_abstract_fields(data)
    if errors:
        return jsonify({'ok': False, 'errors': errors}), 400

    db = get_db()

    # Guard against duplicate submissions from double-clicks, stale tabs, or
    # browser form-resubmission, regardless of what the client-side JS does.
    dedupe_cutoff = (datetime.datetime.utcnow() - datetime.timedelta(seconds=60)).isoformat()
    existing = db.execute(
        "SELECT ref FROM abstracts WHERE email = ? AND title = ? AND body = ? AND created_at > ? "
        "ORDER BY created_at DESC LIMIT 1",
        (v['email'], v['title'], v['body'], dedupe_cutoff),
    ).fetchone()
    if existing:
        return jsonify({'ok': True, 'ref': existing['ref']})

    ref = next_abstract_ref(db)
    db.execute(
        "INSERT INTO abstracts (ref, title, theme, format, body, word_count, ethics_ref, "
        "presenting_author, institution, email, country, career_stage, bio, photo, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Under review', ?)",
        (ref, v['title'], v['theme'], v['format'], v['body'], v['word_count'], v['ethics_ref'],
         v['presenting_author'], v['institution'], v['email'], v['country'], v['career_stage'],
         v['bio'], v['photo'], datetime.datetime.utcnow().isoformat()),
    )
    save_abstract_authors(db, ref, v['authors_data'])
    db.commit()

    edit_link = f'{PUBLIC_BASE}/edit/{ref}?token={make_link_token(v["email"])}'
    send_email(
        v['email'],
        f'ECRI 2026 — abstract received ({ref})',
        f'Hi {v["presenting_author"]},\n\n'
        f'Your abstract "{v["title"]}" has been received and anonymised for blind review.\n'
        f'Reference: {ref}\n\n'
        f'You can edit it until the deadline ({SUBMISSION_DEADLINE}) using this link:\n'
        f'{edit_link}\n'
        f'(valid for 48 hours; request a fresh one any time from My submissions)\n\n'
        f'Outcomes will be shared by 8 October.\n\n'
        f'CeSHHAR Zimbabwe · ECRI 2026',
    )

    return jsonify({'ok': True, 'ref': ref})


@app.route('/ecri/news')
def news_page():
    db = get_db()
    news = db.execute("SELECT * FROM news_items ORDER BY created_at DESC").fetchall()
    gallery = db.execute("SELECT * FROM gallery_images ORDER BY created_at DESC").fetchall()
    return render_template('news.html', news=news, gallery=gallery, tag_style=NEWS_TAG_STYLE)


@app.route('/ecri/api/admin/add-news', methods=['POST'])
@require_role('admin')
def api_admin_add_news():
    data = request.json or {}
    headline = (data.get('headline') or '').strip()
    tag = (data.get('tag') or '').strip()
    body = (data.get('body') or '').strip()
    image = (data.get('image') or '').strip()

    if not headline:
        return jsonify({'ok': False, 'error': 'Headline is required.'}), 400
    if image and (not PHOTO_PATH_RE.match(image) or not os.path.exists(os.path.join(BASE_DIR, 'static', image))):
        return jsonify({'ok': False, 'error': 'Image upload did not complete — please re-upload.'}), 400

    db = get_db()
    db.execute(
        "INSERT INTO news_items (id, tag, headline, body, image, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (os.urandom(8).hex(), tag, headline, body, image, datetime.datetime.utcnow().isoformat()),
    )
    db.commit()
    return jsonify({'ok': True})


@app.route('/ecri/api/admin/delete-news', methods=['POST'])
@require_role('admin')
def api_admin_delete_news():
    data = request.json or {}
    db = get_db()
    db.execute("DELETE FROM news_items WHERE id = ?", (data.get('id'),))
    db.commit()
    return jsonify({'ok': True})


@app.route('/ecri/api/admin/add-gallery-image', methods=['POST'])
@require_role('admin')
def api_admin_add_gallery_image():
    data = request.json or {}
    path = (data.get('path') or '').strip()
    caption = (data.get('caption') or '').strip()

    if not path or not PHOTO_PATH_RE.match(path) or not os.path.exists(os.path.join(BASE_DIR, 'static', path)):
        return jsonify({'ok': False, 'error': 'Upload a photo first.'}), 400

    db = get_db()
    db.execute(
        "INSERT INTO gallery_images (id, path, caption, created_at) VALUES (?, ?, ?, ?)",
        (os.urandom(8).hex(), path, caption, datetime.datetime.utcnow().isoformat()),
    )
    db.commit()
    return jsonify({'ok': True})


@app.route('/ecri/api/admin/delete-gallery-image', methods=['POST'])
@require_role('admin')
def api_admin_delete_gallery_image():
    data = request.json or {}
    db = get_db()
    db.execute("DELETE FROM gallery_images WHERE id = ?", (data.get('id'),))
    db.commit()
    return jsonify({'ok': True})


@app.route('/ecri/downloads')
def downloads_page():
    db = get_db()
    rows = db.execute("SELECT * FROM downloads ORDER BY created_at DESC").fetchall()
    return render_template('downloads.html', rows=rows)


@app.route('/ecri/download/<id>')
def download_file(id):
    db = get_db()
    row = db.execute("SELECT * FROM downloads WHERE id = ?", (id,)).fetchone()
    if not row:
        return render_template('edit_denied.html'), 404
    directory = os.path.join(BASE_DIR, 'static', os.path.dirname(row['file_path']))
    filename = os.path.basename(row['file_path'])
    return send_from_directory(directory, filename, as_attachment=True,
                                download_name=row['original_name'] or filename)


@app.route('/ecri/api/admin/upload-document', methods=['POST'])
@require_role('admin')
def api_admin_upload_document():
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'ok': False, 'error': 'No file received.'}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_DOC_EXT:
        return jsonify({'ok': False, 'error': 'Allowed types: PDF, Word, PowerPoint, Excel.'}), 400

    raw = f.read(MAX_DOC_BYTES + 1)
    if not raw:
        return jsonify({'ok': False, 'error': 'File is empty.'}), 400
    if len(raw) > MAX_DOC_BYTES:
        return jsonify({'ok': False, 'error': 'File must be under 15MB.'}), 400

    filename = f'{os.urandom(12).hex()}{ext}'
    path = os.path.join(BASE_DIR, 'static', 'uploads', 'docs', filename)
    with open(path, 'wb') as out:
        out.write(raw)

    return jsonify({'ok': True, 'file_path': f'uploads/docs/{filename}', 'original_name': f.filename})


@app.route('/ecri/api/admin/add-download', methods=['POST'])
@require_role('admin')
def api_admin_add_download():
    data = request.json or {}
    title = (data.get('title') or '').strip()
    description = (data.get('description') or '').strip()
    file_path = (data.get('file_path') or '').strip()
    original_name = (data.get('original_name') or '').strip()

    if not title:
        return jsonify({'ok': False, 'error': 'Title is required.'}), 400
    if not file_path or not DOC_PATH_RE.match(file_path) or not os.path.exists(os.path.join(BASE_DIR, 'static', file_path)):
        return jsonify({'ok': False, 'error': 'Upload a file first.'}), 400

    db = get_db()
    db.execute(
        "INSERT INTO downloads (id, title, description, file_path, original_name, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (os.urandom(8).hex(), title, description, file_path, original_name,
         datetime.datetime.utcnow().isoformat()),
    )
    db.commit()
    return jsonify({'ok': True})


@app.route('/ecri/api/admin/delete-download', methods=['POST'])
@require_role('admin')
def api_admin_delete_download():
    data = request.json or {}
    db = get_db()
    db.execute("DELETE FROM downloads WHERE id = ?", (data.get('id'),))
    db.commit()
    return jsonify({'ok': True})


@app.route('/ecri/award')
def award_page():
    db = get_db()
    award = db.execute("SELECT * FROM award_page WHERE id = 'main'").fetchone()
    return render_template('award.html', award=award)


@app.route('/ecri/api/admin/save-award', methods=['POST'])
@require_role('admin')
def api_admin_save_award():
    data = request.json or {}
    headline = (data.get('headline') or '').strip()
    body = (data.get('body') or '').strip()
    image = (data.get('image') or '').strip()

    if image and (not PHOTO_PATH_RE.match(image) or not os.path.exists(os.path.join(BASE_DIR, 'static', image))):
        return jsonify({'ok': False, 'error': 'Image upload did not complete — please re-upload.'}), 400

    db = get_db()
    db.execute(
        "INSERT INTO award_page (id, headline, body, image, updated_at) VALUES ('main', ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET headline=excluded.headline, body=excluded.body, "
        "image=excluded.image, updated_at=excluded.updated_at",
        (headline, body, image, datetime.datetime.utcnow().isoformat()),
    )
    db.commit()
    return jsonify({'ok': True})


@app.route('/ecri/api/admin/save-payment-settings', methods=['POST'])
@require_role('admin')
def api_admin_save_payment_settings():
    data = request.json or {}
    db = get_db()
    set_setting(db, 'payment_required', '1' if data.get('required') else '0')
    set_setting(db, 'payment_amount', (data.get('amount') or '').strip())
    set_setting(db, 'payment_instructions', (data.get('instructions') or '').strip())
    db.commit()
    return jsonify({'ok': True})


@app.route('/ecri/api/admin/mark-payment', methods=['POST'])
@require_role('admin')
def api_admin_mark_payment():
    data = request.json or {}
    reg_id = data.get('id')
    status = data.get('status')
    if status not in PAYMENT_STATUSES:
        return jsonify({'ok': False, 'error': 'Unknown status.'}), 400
    db = get_db()
    db.execute("UPDATE registrations SET payment_status = ? WHERE id = ?", (status, reg_id))
    db.commit()
    return jsonify({'ok': True})


ENGAGE_MAX_CHARS = 500


@app.route('/ecri/engage')
def engage_page():
    db = get_db()
    token = request.args.get('token') or ''
    email = resolve_attendee_email(token)
    posts = db.execute(
        "SELECT * FROM engage_posts WHERE approved = 1 ORDER BY created_at DESC"
    ).fetchall()
    replies_by_post = {}
    for r in db.execute("SELECT * FROM engage_replies WHERE approved = 1 ORDER BY created_at").fetchall():
        replies_by_post.setdefault(r['post_id'], []).append(r)
    return render_template(
        'engage.html', posts=posts, replies_by_post=replies_by_post,
        signed_in=bool(email), token=token,
    )


@app.route('/ecri/api/post-engage', methods=['POST'])
def api_post_engage():
    data = request.json or {}
    email = resolve_attendee_email(data.get('token'))
    body = (data.get('body') or '').strip()

    if not email:
        return jsonify({'ok': False, 'error': 'Get a link from Engage to post.'}), 403
    if not body:
        return jsonify({'ok': False, 'error': 'Write something first.'}), 400
    if len(body) > ENGAGE_MAX_CHARS:
        return jsonify({'ok': False, 'error': f'Keep it under {ENGAGE_MAX_CHARS} characters.'}), 400

    db = get_db()
    registration = db.execute(
        "SELECT name FROM registrations WHERE email = ? ORDER BY created_at DESC LIMIT 1", (email,)
    ).fetchone()
    if not registration:
        return jsonify({'ok': False, 'error': 'Only registered attendees can post.'}), 403

    db.execute(
        # Post-moderation, not pre-moderation: live immediately for everyone. A live
        # event wall where posts sit in a queue until admin gets to them defeats the
        # point — the moment (a talk, a panel) has passed by the time it would appear.
        # Admin can still Remove anything inappropriate after the fact.
        "INSERT INTO engage_posts (id, email, author_name, body, approved, created_at) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        (os.urandom(8).hex(), email, registration['name'], body, datetime.datetime.utcnow().isoformat()),
    )
    db.commit()
    return jsonify({'ok': True})


@app.route('/ecri/api/admin/delete-engage-post', methods=['POST'])
@require_role('admin')
def api_admin_delete_engage_post():
    data = request.json or {}
    post_id = data.get('id')
    db = get_db()
    db.execute("DELETE FROM engage_posts WHERE id = ?", (post_id,))
    db.execute("DELETE FROM engage_replies WHERE post_id = ?", (post_id,))  # no orphaned replies
    db.commit()
    return jsonify({'ok': True})


@app.route('/ecri/api/post-engage-reply', methods=['POST'])
def api_post_engage_reply():
    data = request.json or {}
    email = resolve_attendee_email(data.get('token'))
    post_id = (data.get('post_id') or '').strip()
    body = (data.get('body') or '').strip()

    if not email:
        return jsonify({'ok': False, 'error': 'Get a link from Engage to reply.'}), 403
    if not body:
        return jsonify({'ok': False, 'error': 'Write something first.'}), 400
    if len(body) > ENGAGE_MAX_CHARS:
        return jsonify({'ok': False, 'error': f'Keep it under {ENGAGE_MAX_CHARS} characters.'}), 400

    db = get_db()
    post = db.execute("SELECT 1 FROM engage_posts WHERE id = ? AND approved = 1", (post_id,)).fetchone()
    if not post:
        return jsonify({'ok': False, 'error': 'That post no longer exists.'}), 404

    registration = db.execute(
        "SELECT name FROM registrations WHERE email = ? ORDER BY created_at DESC LIMIT 1", (email,)
    ).fetchone()
    if not registration:
        return jsonify({'ok': False, 'error': 'Only registered attendees can reply.'}), 403

    db.execute(
        "INSERT INTO engage_replies (id, post_id, email, author_name, body, approved, created_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?)",
        (os.urandom(8).hex(), post_id, email, registration['name'], body,
         datetime.datetime.utcnow().isoformat()),
    )
    db.commit()
    return jsonify({'ok': True})


@app.route('/ecri/api/admin/delete-engage-reply', methods=['POST'])
@require_role('admin')
def api_admin_delete_engage_reply():
    data = request.json or {}
    db = get_db()
    db.execute("DELETE FROM engage_replies WHERE id = ?", (data.get('id'),))
    db.commit()
    return jsonify({'ok': True})


@app.route('/ecri/research-in-focus')
def research_focus_page():
    db = get_db()
    rows = db.execute(
        "SELECT rf.*, a.title AS abstract_title, a.theme, a.presenting_author, a.institution "
        "FROM research_focus rf JOIN abstracts a ON a.ref = rf.abstract_ref "
        "ORDER BY rf.created_at DESC"
    ).fetchall()
    return render_template('research_focus.html', rows=rows, theme_style=THEME_STYLE)


@app.route('/ecri/api/admin/save-research-focus', methods=['POST'])
@require_role('admin')
def api_admin_save_research_focus():
    data = request.json or {}
    abstract_ref = (data.get('abstract_ref') or '').strip()
    headline = (data.get('headline') or '').strip()
    q_about = (data.get('q_about') or '').strip()
    q_problem = (data.get('q_problem') or '').strip()
    q_matters = (data.get('q_matters') or '').strip()
    q_hoping = (data.get('q_hoping') or '').strip()
    credit = (data.get('credit') or '').strip()

    db = get_db()
    abstract = db.execute(
        "SELECT ref FROM abstracts WHERE ref = ? AND status IN ('Accepted — oral', 'Accepted — poster')",
        (abstract_ref,),
    ).fetchone()
    if not abstract:
        return jsonify({'ok': False, 'error': 'Choose an accepted abstract.'}), 400
    if not q_about and not q_problem and not q_matters and not q_hoping:
        return jsonify({'ok': False, 'error': 'Answer at least one question.'}), 400

    db.execute(
        "INSERT INTO research_focus (abstract_ref, headline, q_about, q_problem, q_matters, q_hoping, credit, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(abstract_ref) DO UPDATE SET headline=excluded.headline, q_about=excluded.q_about, "
        "q_problem=excluded.q_problem, q_matters=excluded.q_matters, q_hoping=excluded.q_hoping, credit=excluded.credit",
        (abstract_ref, headline, q_about, q_problem, q_matters, q_hoping, credit,
         datetime.datetime.utcnow().isoformat()),
    )
    db.commit()
    return jsonify({'ok': True})


@app.route('/ecri/api/admin/delete-research-focus', methods=['POST'])
@require_role('admin')
def api_admin_delete_research_focus():
    data = request.json or {}
    db = get_db()
    db.execute("DELETE FROM research_focus WHERE abstract_ref = ?", (data.get('abstract_ref'),))
    db.commit()
    return jsonify({'ok': True})


@app.route('/ecri/researchers')
def researchers_page():
    db = get_db()
    # LEFT JOIN sessions so each researcher card can show its own scheduled slot (once
    # allocated) without a separate lookup — keeps this page and Programme in sync
    # instead of two disconnected views of the same accepted abstracts.
    rows = db.execute(
        "SELECT a.*, s.day AS session_day, s.time AS session_time, s.room AS session_room "
        "FROM abstracts a LEFT JOIN sessions s ON s.abstract_ref = a.ref "
        "WHERE a.status IN ('Accepted — oral', 'Accepted — poster') "
        "ORDER BY a.presenting_author"
    ).fetchall()
    by_theme = {}
    for r in rows:
        by_theme.setdefault(r['theme'], []).append(r)
    groups = [{'theme': t, 'rows': by_theme[t]} for t in THEMES if t in by_theme]
    return render_template('researchers.html', groups=groups, has_any=bool(rows), theme_style=THEME_STYLE)


@app.route('/ecri/my-submissions', methods=['GET'])
def my_submissions_page():
    email = resolve_attendee_email(request.args.get('token'))
    rows = []
    if email:
        db = get_db()
        rows = db.execute(
            "SELECT * FROM abstracts WHERE email = ? ORDER BY created_at DESC", (email,)
        ).fetchall()
    return render_template(
        'my_submissions.html',
        email=email,
        token=request.args.get('token'),
        rows=rows,
        theme_style=THEME_STYLE,
        link_sent=request.args.get('sent') == '1',
    )


@app.route('/ecri/api/request-link', methods=['POST'])
def api_request_link():
    data = request.json or {}
    email = (data.get('email') or '').strip()
    if not email or not EMAIL_RE.match(email):
        return jsonify({'ok': False, 'errors': {'email': 'Enter a valid email address.'}}), 400

    db = get_db()
    has_submissions = db.execute(
        "SELECT 1 FROM abstracts WHERE email = ? LIMIT 1", (email,)
    ).fetchone()
    if has_submissions:
        link = f'{PUBLIC_BASE}/my-submissions?token={make_link_token(email)}'
        send_email(
            email,
            'ECRI 2026 — your My submissions link',
            f'Use this link to view and edit your ECRI 2026 abstract submissions:\n{link}\n\n'
            f'(valid for 48 hours)\n\nCeSHHAR Zimbabwe · ECRI 2026',
        )
    # Always return ok, regardless of whether that email has submissions,
    # so this endpoint can't be used to check who has submitted.
    return jsonify({'ok': True})


### ATTENDEE IDENTITY: My schedule (starred sessions) ###

@app.route('/ecri/my-schedule')
def my_schedule_page():
    email = resolve_attendee_email(request.args.get('token'))
    rows = []
    registration = None
    payment_amount = ''
    payment_instructions = ''
    if email:
        db = get_db()
        rows = db.execute(
            "SELECT s.*, a.photo AS presenter_photo FROM session_stars st "
            "JOIN sessions s ON s.id = st.session_id "
            "LEFT JOIN abstracts a ON a.ref = s.abstract_ref "
            "WHERE st.email = ? ORDER BY s.day, s.time",
            (email,),
        ).fetchall()
        registration = db.execute(
            "SELECT * FROM registrations WHERE email = ? ORDER BY created_at DESC LIMIT 1", (email,)
        ).fetchone()
        payment_amount = get_setting(db, 'payment_amount', '')
        payment_instructions = get_setting(db, 'payment_instructions', '')
    return render_template(
        'my_schedule.html',
        email=email,
        token=request.args.get('token') or '',
        rows=rows,
        theme_style=THEME_STYLE,
        registration=registration,
        payment_amount=payment_amount,
        payment_instructions=payment_instructions,
    )


@app.route('/ecri/my-schedule/logout')
def my_schedule_logout():
    session.pop('attendee_email', None)
    return redirect(url_for('my_schedule_page'))


@app.route('/ecri/api/request-schedule-link', methods=['POST'])
def api_request_schedule_link():
    data = request.json or {}
    email = (data.get('email') or '').strip()
    if not email or not EMAIL_RE.match(email):
        return jsonify({'ok': False, 'errors': {'email': 'Enter a valid email address.'}}), 400

    db = get_db()
    is_registered = db.execute(
        "SELECT 1 FROM registrations WHERE email = ? LIMIT 1", (email,)
    ).fetchone()
    if is_registered:
        link = f'{PUBLIC_BASE}/programme?token={make_link_token(email)}'
        send_email(
            email,
            'ECRI 2026 — your programme link',
            f'Use this link to browse the programme and save session reminders:\n{link}\n\n'
            f'Click it once and this device will remember you afterwards — no need to '
            f'bookmark or save it. If you switch devices or clear your browser, just '
            f'request a fresh link from Programme or My schedule any time.\n\n'
            f'CeSHHAR Zimbabwe · ECRI 2026',
        )
    # Always ok — no confirmation of who is registered.
    return jsonify({'ok': True})


@app.route('/ecri/api/star-session', methods=['POST'])
def api_star_session():
    data = request.json or {}
    email = resolve_attendee_email(data.get('token'))
    session_id = data.get('session_id')
    star = bool(data.get('star'))

    if not email:
        return jsonify({'ok': False, 'error': 'Your link has expired. Request a new one from Programme.'}), 403

    db = get_db()
    is_registered = db.execute(
        "SELECT 1 FROM registrations WHERE email = ? LIMIT 1", (email,)
    ).fetchone()
    if not is_registered:
        return jsonify({'ok': False, 'error': 'Only registered attendees can save reminders.'}), 403

    if star:
        db.execute(
            "INSERT OR IGNORE INTO session_stars (email, session_id, created_at) VALUES (?, ?, ?)",
            (email, session_id, datetime.datetime.utcnow().isoformat()),
        )
    else:
        db.execute("DELETE FROM session_stars WHERE email = ? AND session_id = ?", (email, session_id))
    db.commit()
    return jsonify({'ok': True})


@app.route('/ecri/api/submit-payment', methods=['POST'])
def api_submit_payment():
    data = request.json or {}
    email = resolve_attendee_email(data.get('token'))
    reference = (data.get('reference') or '').strip()
    proof = (data.get('proof') or '').strip()

    if not email:
        return jsonify({'ok': False, 'error': 'Your link has expired. Request a new one from My schedule.'}), 403
    if not reference and not proof:
        return jsonify({'ok': False, 'error': 'Add a payment reference or upload a screenshot.'}), 400
    if proof and (not PHOTO_PATH_RE.match(proof) or not os.path.exists(os.path.join(BASE_DIR, 'static', proof))):
        return jsonify({'ok': False, 'error': 'Screenshot upload did not complete — please re-upload.'}), 400

    db = get_db()
    row = db.execute(
        "SELECT id FROM registrations WHERE email = ? ORDER BY created_at DESC LIMIT 1", (email,)
    ).fetchone()
    if not row:
        return jsonify({'ok': False, 'error': 'No registration found for this account.'}), 404

    db.execute(
        "UPDATE registrations SET payment_status = 'Submitted', payment_reference = ?, payment_proof = ? WHERE id = ?",
        (reference, proof, row['id']),
    )
    db.commit()
    return jsonify({'ok': True})


### STAFF AUTH (Reviewer / Chair / Admin) ###

def staff_default_landing(roles):
    """Where a staff member lands after logging in or setting their password —
    whichever of their roles gives the most access, so a reviewer-only account
    doesn't land on the admin-only dashboard and immediately get bounced."""
    default_landing = {'admin': '/ecri/admin', 'chair': '/ecri/allocation', 'reviewer': '/ecri/review'}
    for r in ['admin', 'chair', 'reviewer']:
        if r in roles:
            return default_landing[r]
    return '/ecri/admin'


@app.route('/ecri/staff/login', methods=['GET'])
def staff_login_page():
    return render_template('staff_login.html', next=request.args.get('next') or '')


@app.route('/ecri/api/staff-login', methods=['POST'])
def api_staff_login():
    data = request.json or {}
    email = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    next_url = data.get('next') or ''

    db = get_db()
    row = db.execute("SELECT * FROM staff_users WHERE email = ?", (email,)).fetchone()
    if not row or not check_password_hash(row['password_hash'], password):
        return jsonify({'ok': False, 'errors': {'password': 'Invalid email or password.'}}), 401

    session['staff_email'] = row['email']
    roles = (row['roles'] or '').split(',')
    landing = pub_path(staff_default_landing(roles))
    if next_url.startswith('/ecri/') or (is_clean_host() and next_url.startswith('/') and not next_url.startswith('//')):
        landing = pub_path(next_url)
    return jsonify({'ok': True, 'redirect': landing, 'name': row['name'], 'roles': roles})


@app.route('/ecri/staff/logout')
def staff_logout():
    session.pop('staff_email', None)
    return redirect(url_for('staff_login_page'))


### REVIEWER: queue + scoring sheet ###

@app.route('/ecri/review')
@require_role('reviewer')
def review_queue():
    staff = current_staff()
    db = get_db()

    staff_roles = (staff['roles'] or '').split(',')
    tracks = [t for t in (staff['tracks'] or '').split('|') if t]
    full_access = 'admin' in staff_roles or 'chair' in staff_roles

    if full_access:
        abstracts = db.execute(
            "SELECT * FROM abstracts WHERE status = 'Under review' ORDER BY created_at"
        ).fetchall()
    elif tracks:
        placeholders = ','.join('?' for _ in tracks)
        abstracts = db.execute(
            f"SELECT * FROM abstracts WHERE status = 'Under review' AND theme IN ({placeholders}) "
            f"ORDER BY created_at",
            tracks,
        ).fetchall()
    else:
        abstracts = []

    my_reviews = {
        r['abstract_ref']: r for r in db.execute(
            "SELECT * FROM reviews WHERE reviewer_email = ?", (staff['email'],)
        ).fetchall()
    }
    rows = []
    for a in abstracts:
        rev = my_reviews.get(a['ref'])
        mean = None
        if rev and all(rev[k] is not None for k in CRITERIA_KEYS):
            mean = sum(rev[k] for k in CRITERIA_KEYS) / len(CRITERIA_KEYS)
        rows.append({'abstract': a, 'mean': mean, 'scored': mean is not None})
    scored_count = sum(1 for r in rows if r['scored'])
    return render_template(
        'review_queue.html',
        staff=staff,
        rows=rows,
        scored_count=scored_count,
        total_count=len(rows),
        theme_style=THEME_STYLE,
        tracks=tracks,
        full_access=full_access,
        no_tracks_assigned=(not full_access and not tracks),
    )


@app.route('/ecri/review/<ref>')
@require_role('reviewer')
def scoring_sheet(ref):
    staff = current_staff()
    db = get_db()
    abstract = db.execute("SELECT * FROM abstracts WHERE ref = ?", (ref,)).fetchone()
    if not abstract or not staff_can_access_theme(staff, abstract['theme']):
        return render_template('edit_denied.html'), 404
    review = db.execute(
        "SELECT * FROM reviews WHERE abstract_ref = ? AND reviewer_email = ?", (ref, staff['email'])
    ).fetchone()
    notes = db.execute(
        "SELECT * FROM abstract_notes WHERE abstract_ref = ? ORDER BY created_at", (ref,)
    ).fetchall()
    return render_template(
        'scoring_sheet.html',
        staff=staff,
        abstract=abstract,
        # dict(), not the raw Row — Jinja's tojson can't serialize sqlite3.Row directly
        review=(dict(review) if review else None),
        criteria=CRITERIA,
        recommendations=RECOMMENDATIONS,
        theme_style=THEME_STYLE,
        notes=notes,
    )


@app.route('/ecri/api/notes/<ref>', methods=['POST'])
@require_role('reviewer')
def api_add_note(ref):
    staff = current_staff()
    db = get_db()
    abstract = db.execute("SELECT theme FROM abstracts WHERE ref = ?", (ref,)).fetchone()
    if not abstract or not staff_can_access_theme(staff, abstract['theme']):
        return jsonify({'ok': False, 'error': 'You are not assigned to this track.'}), 403

    body = (request.json or {}).get('body', '').strip()
    if not body:
        return jsonify({'ok': False, 'error': 'Note cannot be empty.'}), 400

    db.execute(
        "INSERT INTO abstract_notes (id, abstract_ref, author_email, author_name, body, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (os.urandom(8).hex(), ref, staff['email'], staff['name'], body, datetime.datetime.utcnow().isoformat()),
    )
    db.commit()
    return jsonify({'ok': True})


@app.route('/ecri/api/review/<ref>', methods=['POST'])
@require_role('reviewer')
def api_submit_review(ref):
    staff = current_staff()
    db = get_db()
    abstract = db.execute("SELECT theme FROM abstracts WHERE ref = ?", (ref,)).fetchone()
    if not abstract or not staff_can_access_theme(staff, abstract['theme']):
        return jsonify({'ok': False, 'error': 'You are not assigned to this track.'}), 403

    data = request.json or {}

    scores = {}
    for c in CRITERIA:
        v = data.get(c['key'])
        if v not in (1, 2, 3, 4, 5):
            return jsonify({'ok': False, 'error': f"All {len(CRITERIA)} criteria must be scored 1–5."}), 400
        scores[c['key']] = v

    recommendation = data.get('recommendation')
    if recommendation not in RECOMMENDATIONS:
        return jsonify({'ok': False, 'error': 'Choose a recommendation.'}), 400

    comment = (data.get('comment') or '').strip()
    existing = db.execute(
        "SELECT id FROM reviews WHERE abstract_ref = ? AND reviewer_email = ?", (ref, staff['email'])
    ).fetchone()
    now = datetime.datetime.utcnow().isoformat()
    score_values = [scores[k] for k in CRITERIA_KEYS]
    if existing:
        set_clause = ', '.join(f'{k}=?' for k in CRITERIA_KEYS)
        db.execute(
            f"UPDATE reviews SET {set_clause}, comment=?, recommendation=?, submitted_at=? WHERE id=?",
            (*score_values, comment, recommendation, now, existing['id']),
        )
    else:
        cols = ', '.join(CRITERIA_KEYS)
        placeholders = ', '.join('?' for _ in CRITERIA_KEYS)
        db.execute(
            f"INSERT INTO reviews (id, abstract_ref, reviewer_email, {cols}, comment, recommendation, submitted_at) "
            f"VALUES (?, ?, ?, {placeholders}, ?, ?, ?)",
            (os.urandom(8).hex(), ref, staff['email'], *score_values, comment, recommendation, now),
        )
    db.commit()
    return jsonify({'ok': True})


### CHAIR: allocation board ###

@app.route('/ecri/allocation')
@require_role('chair')
def allocation_board():
    db = get_db()
    accepted = db.execute(
        "SELECT * FROM abstracts WHERE status IN ('Accepted — oral', 'Accepted — poster')"
    ).fetchall()

    allocations = {r['slot_id']: r['abstract_ref'] for r in db.execute("SELECT * FROM allocations").fetchall()}
    allocated_refs = set(allocations.values())

    def mean_score(ref):
        row = db.execute(
            f"SELECT AVG({mean_score_sql()}) AS m FROM reviews WHERE abstract_ref = ?", (ref,)
        ).fetchone()
        return row['m']

    pool = sorted(
        [{'abstract': a, 'mean': mean_score(a['ref'])} for a in accepted if a['ref'] not in allocated_refs],
        key=lambda x: (x['mean'] is None, -(x['mean'] or 0)),
    )

    abstracts_by_ref = {a['ref']: a for a in accepted}
    slot_rows = db.execute("SELECT * FROM allocation_slots ORDER BY day, time").fetchall()
    slots_by_day = {}
    for s in slot_rows:
        ref = allocations.get(s['id'])
        slots_by_day.setdefault(s['day'], []).append({
            'id': s['id'], 'time': s['time'], 'room': s['room'],
            'abstract': abstracts_by_ref.get(ref) if ref else None,
        })

    placed = len(allocated_refs)
    return render_template(
        'allocation_board.html',
        pool=pool,
        slots_by_day=slots_by_day,
        placed=placed,
        total=len(slot_rows),
        theme_style=THEME_STYLE,
    )


@app.route('/ecri/api/allocate', methods=['POST'])
@require_role('chair')
def api_allocate():
    data = request.json or {}
    ref = data.get('ref')
    slot_id = data.get('slot_id')
    db = get_db()
    if not db.execute("SELECT 1 FROM allocation_slots WHERE id = ?", (slot_id,)).fetchone():
        return jsonify({'ok': False, 'error': 'Unknown slot.'}), 400

    abstract = db.execute(
        "SELECT * FROM abstracts WHERE ref = ? AND status IN ('Accepted — oral', 'Accepted — poster')", (ref,)
    ).fetchone()
    if not abstract:
        return jsonify({'ok': False, 'error': 'Abstract not found or not accepted.'}), 400

    already = db.execute("SELECT 1 FROM allocations WHERE abstract_ref = ?", (ref,)).fetchone()
    if already:
        return jsonify({'ok': False, 'error': 'That abstract is already placed in a slot.'}), 400

    db.execute(
        "INSERT OR REPLACE INTO allocations (slot_id, abstract_ref, allocated_at) VALUES (?, ?, ?)",
        (slot_id, ref, datetime.datetime.utcnow().isoformat()),
    )
    db.commit()
    return jsonify({'ok': True})


@app.route('/ecri/api/deallocate', methods=['POST'])
@require_role('chair')
def api_deallocate():
    data = request.json or {}
    slot_id = data.get('slot_id')
    db = get_db()
    db.execute("DELETE FROM allocations WHERE slot_id = ?", (slot_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/ecri/api/admin/add-allocation-slot', methods=['POST'])
@require_role('chair')
def api_add_allocation_slot():
    data = request.json or {}
    day = data.get('day')
    time_ = normalize_session_time(data.get('time') or '')
    room = (data.get('room') or '').strip()
    if day not in (1, 2) or not time_:
        return jsonify({'ok': False, 'error': 'Day and time are required.'}), 400

    db = get_db()
    db.execute(
        "INSERT INTO allocation_slots (id, day, time, room, created_at) VALUES (?, ?, ?, ?, ?)",
        (os.urandom(8).hex(), day, time_, room, datetime.datetime.utcnow().isoformat()),
    )
    db.commit()
    return jsonify({'ok': True})


@app.route('/ecri/api/admin/generate-allocation-grid', methods=['POST'])
@require_role('chair')
def api_generate_allocation_grid():
    data = request.json or {}
    day = data.get('day')
    start = normalize_session_time(data.get('start') or '')
    end = normalize_session_time(data.get('end') or '')
    interval = data.get('interval_minutes') or 15
    room = (data.get('room') or '').strip()

    if day not in (1, 2):
        return jsonify({'ok': False, 'error': 'Choose a day.'}), 400
    try:
        start_h, start_m = (int(x) for x in start.split(':'))
        end_h, end_m = (int(x) for x in end.split(':'))
        interval = int(interval)
    except (ValueError, AttributeError):
        return jsonify({'ok': False, 'error': 'Enter valid start/end times.'}), 400
    if interval <= 0:
        return jsonify({'ok': False, 'error': 'Interval must be positive.'}), 400

    start_minutes, end_minutes = start_h * 60 + start_m, end_h * 60 + end_m
    if end_minutes <= start_minutes:
        return jsonify({'ok': False, 'error': 'End time must be after start time.'}), 400

    db = get_db()
    created = 0
    for minutes in range(start_minutes, end_minutes, interval):
        time_str = f'{minutes // 60:02d}:{minutes % 60:02d}'
        db.execute(
            "INSERT INTO allocation_slots (id, day, time, room, created_at) VALUES (?, ?, ?, ?, ?)",
            (os.urandom(8).hex(), day, time_str, room, datetime.datetime.utcnow().isoformat()),
        )
        created += 1
    db.commit()
    return jsonify({'ok': True, 'created': created})


@app.route('/ecri/api/admin/delete-allocation-slot', methods=['POST'])
@require_role('chair')
def api_delete_allocation_slot():
    data = request.json or {}
    slot_id = data.get('id')
    db = get_db()
    if db.execute("SELECT 1 FROM allocations WHERE slot_id = ?", (slot_id,)).fetchone():
        return jsonify({'ok': False, 'error': 'Free this slot before removing it.'}), 400
    db.execute("DELETE FROM allocation_slots WHERE id = ?", (slot_id,))
    db.commit()
    return jsonify({'ok': True})


### ADMIN: dashboard, decisions, announcements, staff management ###

def compute_decision_groups(db):
    decisions_raw = db.execute(
        f"SELECT a.*, (SELECT AVG({mean_score_sql()}) FROM reviews WHERE abstract_ref = a.ref) AS mean, "
        "(SELECT COUNT(*) FROM reviews WHERE abstract_ref = a.ref) AS review_count "
        "FROM abstracts a ORDER BY a.created_at"
    ).fetchall()
    decisions = []
    for d in decisions_raw:
        recs = [r['recommendation'] for r in db.execute(
            "SELECT recommendation FROM reviews WHERE abstract_ref = ?", (d['ref'],)
        ).fetchall()]
        # How many reviews are expected for this abstract's track — could legitimately
        # be 1 if that's all the track has assigned. Falls back to "whatever's already
        # in" if nobody's currently assigned to the track (e.g. reassigned since review),
        # so a real review is never stuck waiting on a target that no longer exists.
        required = assigned_reviewer_count(db, d['theme'])
        # 0 assigned is a staffing gap, not "this track only needs 1 reviewer" — don't
        # quietly treat a stray review (e.g. left over from before a reassignment) as a
        # finished decision just because nobody currently covers the track.
        unassigned = required == 0 and len(recs) > 0
        complete = required > 0 and len(recs) >= required
        split = len(set(recs)) > 1  # disagreement is worth surfacing as soon as it exists
        unanimous = complete and not split
        decisions.append({
            'row': d,
            'recommendations': recs,
            'required': required,
            'unanimous': unanimous,
            'split': split,
            'unassigned': unassigned,
            'awaiting_more': 0 < len(recs) < required and not split,
            'suggested_status': RECOMMENDATION_TO_STATUS.get(recs[0]) if unanimous else None,
        })

    # Grouped by track and ranked by mean score (highest first, unscored last) so
    # candidates within a track can be compared against each other before deciding who
    # makes the cut — rather than a flat list in submission order that gives no sense of
    # how a given abstract stacks up against its actual competition for the same slots.
    by_track = {}
    for d in decisions:
        by_track.setdefault(d['row']['theme'], []).append(d)
    for group in by_track.values():
        group.sort(key=lambda d: (d['row']['mean'] is None, -(d['row']['mean'] or 0)))
    decision_groups = [
        {'theme': t, 'decisions': by_track[t]} for t in THEMES if t in by_track
    ]
    # Any theme outside the normal set (shouldn't happen, but don't silently drop it)
    for t in by_track:
        if t not in THEMES:
            decision_groups.append({'theme': t, 'decisions': by_track[t]})
    return decision_groups


@app.route('/ecri/admin')
@require_role('admin')
def admin_dashboard():
    db = get_db()
    registered = db.execute("SELECT COUNT(*) c FROM registrations").fetchone()['c']
    abstracts_total = db.execute("SELECT COUNT(*) c FROM abstracts").fetchone()['c']
    accepted_oral = db.execute("SELECT COUNT(*) c FROM abstracts WHERE status = 'Accepted — oral'").fetchone()['c']
    reviews_count = db.execute("SELECT COUNT(*) c FROM reviews").fetchone()['c']

    by_theme = []
    max_count = 1
    for t in THEMES:
        c = db.execute("SELECT COUNT(*) c FROM abstracts WHERE theme = ?", (t,)).fetchone()['c']
        by_theme.append({'theme': t, 'count': c})
        max_count = max(max_count, c)

    announcements = db.execute("SELECT * FROM announcements ORDER BY sent_at DESC").fetchall()

    return render_template(
        'admin_dashboard.html',
        active='overview',
        registered=registered,
        capacity=EVENT_CAPACITY,
        abstracts_total=abstracts_total,
        accepted_oral=accepted_oral,
        reviews_count=reviews_count,
        by_theme=by_theme,
        max_count=max_count,
        theme_style=THEME_STYLE,
        announcements=announcements,
    )


@app.route('/ecri/admin/decisions')
@require_role('admin')
def admin_decisions():
    db = get_db()
    return render_template(
        'admin_decisions.html',
        active='decisions',
        theme_style=THEME_STYLE,
        decision_groups=compute_decision_groups(db),
        decision_statuses=DECISION_STATUSES,
    )


@app.route('/ecri/admin/programme')
@require_role('admin')
def admin_programme():
    db = get_db()
    accepted_for_sessions = db.execute(
        "SELECT ref, title, presenting_author, theme, format FROM abstracts "
        "WHERE status IN ('Accepted — oral', 'Accepted — poster') "
        "AND ref NOT IN (SELECT abstract_ref FROM sessions WHERE abstract_ref IS NOT NULL) "
        "ORDER BY title"
    ).fetchall()
    sessions_list = db.execute("SELECT * FROM sessions ORDER BY day, time").fetchall()
    return render_template(
        'admin_programme.html',
        active='programme',
        session_themes=SESSION_THEMES,
        sessions_list=sessions_list,
        accepted_for_sessions=accepted_for_sessions,
    )


@app.route('/ecri/admin/content')
@require_role('admin')
def admin_content():
    db = get_db()
    news_list = db.execute("SELECT * FROM news_items ORDER BY created_at DESC").fetchall()
    gallery_list = db.execute("SELECT * FROM gallery_images ORDER BY created_at DESC").fetchall()
    accepted_abstracts = db.execute(
        "SELECT a.ref, a.title, a.presenting_author, rf.headline, rf.q_about, rf.q_problem, "
        "rf.q_matters, rf.q_hoping, rf.credit "
        "FROM abstracts a LEFT JOIN research_focus rf ON rf.abstract_ref = a.ref "
        "WHERE a.status IN ('Accepted — oral', 'Accepted — poster') ORDER BY a.title"
    ).fetchall()
    research_focus_list = db.execute(
        "SELECT rf.*, a.title AS abstract_title FROM research_focus rf "
        "JOIN abstracts a ON a.ref = rf.abstract_ref ORDER BY rf.created_at DESC"
    ).fetchall()
    downloads_list = db.execute("SELECT * FROM downloads ORDER BY created_at DESC").fetchall()
    award = db.execute("SELECT * FROM award_page WHERE id = 'main'").fetchone()
    return render_template(
        'admin_content.html',
        active='content',
        news_list=news_list,
        gallery_list=gallery_list,
        news_tags=NEWS_TAGS,
        accepted_abstracts=accepted_abstracts,
        research_focus_list=research_focus_list,
        downloads_list=downloads_list,
        award=award,
    )


@app.route('/ecri/admin/engage')
@require_role('admin')
def admin_engage():
    db = get_db()
    # Post-moderation: everything is live immediately, so this is just a flat management
    # list with Remove, not an approval queue.
    approved_posts = db.execute(
        "SELECT * FROM engage_posts ORDER BY created_at DESC"
    ).fetchall()
    approved_replies = db.execute(
        "SELECT er.*, ep.body AS post_body FROM engage_replies er "
        "JOIN engage_posts ep ON ep.id = er.post_id "
        "ORDER BY er.created_at DESC"
    ).fetchall()
    return render_template(
        'admin_engage.html',
        active='engage',
        approved_posts=approved_posts,
        approved_replies=approved_replies,
    )


@app.route('/ecri/admin/staff')
@require_role('admin')
def admin_staff():
    db = get_db()
    reviewers = db.execute(
        "SELECT email, name, roles, tracks FROM staff_users WHERE roles LIKE '%reviewer%' ORDER BY name"
    ).fetchall()
    return render_template('admin_staff.html', active='staff', reviewers=reviewers, themes=THEMES)


REGISTRATION_EXPORT_COLUMNS = [
    ('code', 'Participant_ID'), ('created_at', 'Registration_Date'),
    ('first_name', 'First_Name'), ('surname', 'Surname'), ('email', 'Email'),
    ('phone', 'Phone'), ('institution', 'Organisation'), ('department', 'Department'),
    ('job_title', 'Job_Title'), ('professional_category', 'Professional_Category'),
    ('participation_category', 'Participation_Category'), ('country', 'Country'),
    ('city', 'City'), ('attendance', 'Attendance_Mode'), ('days_attending', 'Days_Attending'),
    ('research_interests', 'Thematic_Interests'), ('presentation_type', 'Presentation_Type'),
    ('presentation_title', 'Abstract_Title'), ('abstract_id_ref', 'Abstract_ID'),
    ('dietary_requirements', 'Dietary_Request'), ('accessibility_requirements', 'Accessibility_Request'),
    ('cocktail_attendance', 'Cocktail_Attendance'), ('travelling_outside_harare', 'Travelling_From_Outside_Harare'),
    ('consent_photo', 'Consent_Photo'), ('consent_comms', 'Consent_Email'),
    ('payment_status', 'Payment_Status'), ('checked_in_at', 'Check_In_Status'),
    ('certificate_issued_at', 'Certificate_Status'),
]


@app.route('/ecri/admin/registrations')
@require_role('admin')
def admin_registrations():
    db = get_db()
    rows = db.execute("SELECT * FROM registrations ORDER BY created_at DESC").fetchall()
    checked_in_count = sum(1 for r in rows if r['checked_in_at'])
    payment_required = get_setting(db, 'payment_required', '0') == '1'
    payment_amount = get_setting(db, 'payment_amount', '')
    payment_instructions = get_setting(db, 'payment_instructions', '')
    # Only registrations payment actually applies to — 'Not required' rows are just
    # noise here, since they registered before payment was ever turned on.
    payments_list = db.execute(
        "SELECT * FROM registrations WHERE payment_status != 'Not required' ORDER BY created_at DESC"
    ).fetchall()
    return render_template(
        'admin_registrations.html',
        active='registrations',
        rows=rows,
        total=len(rows),
        checked_in_count=checked_in_count,
        professional_categories=PROFESSIONAL_CATEGORIES,
        participation_categories=PARTICIPATION_CATEGORIES,
        payment_required=payment_required,
        payment_amount=payment_amount,
        payment_instructions=payment_instructions,
        payments_list=payments_list,
        payment_statuses=PAYMENT_STATUSES,
    )


@app.route('/ecri/admin/checkin')
@require_role('admin')
def admin_checkin_page():
    db = get_db()
    checked_in_count = db.execute(
        "SELECT COUNT(*) c FROM registrations WHERE checked_in_at IS NOT NULL"
    ).fetchone()['c']
    total = db.execute("SELECT COUNT(*) c FROM registrations").fetchone()['c']
    return render_template('admin_checkin.html', active='checkin', checked_in_count=checked_in_count, total=total)


@app.route('/ecri/api/admin/export-registrations')
@require_role('admin')
def api_admin_export_registrations():
    db = get_db()
    rows = db.execute("SELECT * FROM registrations ORDER BY created_at").fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = 'Registrations'
    headers = [label for _, label in REGISTRATION_EXPORT_COLUMNS]
    ws.append(headers)
    for row in rows:
        ws.append([row[col] if row[col] is not None else '' for col, _ in REGISTRATION_EXPORT_COLUMNS])
    for i, header in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(i)].width = max(14, len(header) + 2)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return app.response_class(
        buf.getvalue(),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': 'attachment; filename=ecri-2026-registrations.xlsx'},
    )


def fit_text(c, text, font_name, font_size, max_width):
    """Truncates text with an ellipsis, at a fixed font size, so it never overlaps
    whatever sits to its right (e.g. a badge's logo/QR corners)."""
    text = text or ''
    if c.stringWidth(text, font_name, font_size) <= max_width:
        return text
    while text and c.stringWidth(text + '…', font_name, font_size) > max_width:
        text = text[:-1]
    return (text + '…') if text else ''


def draw_badge(c, x, y, width, height, reg):
    """Draws one badge (name, org, category, QR, CeSHHAR logo) with its bottom-left
    corner at (x, y). Logo sits top-right, QR bottom-right, each in its own reserved
    corner so neither ever overlaps the name/institution text on the left."""
    c.setFillColorRGB(0.043, 0.129, 0.251)  # var(--ink) navy
    c.rect(x, y, width, height, fill=1, stroke=0)

    # Logo, top-right, on a small white chip so it stays legible on navy
    logo_w, logo_h = 20 * mm, 13 * mm
    logo_x, logo_y = x + width - logo_w - 5 * mm, y + height - logo_h - 5 * mm
    c.setFillColorRGB(1, 1, 1)
    c.roundRect(logo_x - 2 * mm, logo_y - 2 * mm, logo_w + 4 * mm, logo_h + 4 * mm, 2 * mm, fill=1, stroke=0)
    if os.path.exists(LOGO_PATH):
        c.drawImage(LOGO_PATH, logo_x, logo_y, width=logo_w, height=logo_h,
                    preserveAspectRatio=True, anchor='c', mask='auto')

    # QR, bottom-right — reserved column, never shared with text
    qr_img = qrcode.make(reg['code'])
    qr_buf = io.BytesIO()
    qr_img.save(qr_buf, format='PNG')
    qr_buf.seek(0)
    qr_size = 22 * mm
    qr_x = x + width - qr_size - 6 * mm
    c.drawImage(ImageReader(qr_buf), qr_x, y + 4 * mm, width=qr_size, height=qr_size, mask='auto')

    # Name/institution — left column, width capped so it can never reach the logo chip
    text_max_x = logo_x - 4 * mm
    text_max_width = text_max_x - (x + 8 * mm)
    c.setFillColorRGB(1, 1, 1)
    c.setFont('Helvetica-Bold', 14)
    display_name = reg['preferred_name'] or reg['name']
    c.drawString(x + 8 * mm, y + height - 12 * mm, fit_text(c, display_name, 'Helvetica-Bold', 14, text_max_width))
    c.setFont('Helvetica', 9)
    c.drawString(x + 8 * mm, y + height - 18 * mm,
                 fit_text(c, reg['institution'] or '', 'Helvetica', 9, text_max_width))

    # Category/event line — bottom-left, width capped so it can never reach the QR
    bottom_max_width = qr_x - 4 * mm - (x + 8 * mm)
    c.setFont('Helvetica', 8)
    c.setFillColorRGB(0.73, 0.8, 0.88)
    c.drawString(x + 8 * mm, y + 8 * mm,
                 fit_text(c, reg['participation_category'] or 'Attendee', 'Helvetica', 8, bottom_max_width))
    c.drawString(x + 8 * mm, y + 4 * mm,
                 fit_text(c, 'ECRI 2026 · 22-23 Oct · Harare', 'Helvetica', 8, bottom_max_width))


def draw_certificate(c, reg):
    """Draws one certificate of participation on the current landscape A4 page.
    Layout borrows the structure of CeSHHAR's own GeoAI certificates (logo corner,
    bold heading, decorative corner accents, name in a display font, single signature
    block) using only reportlab primitives — no external art assets beyond the logo."""
    page_w, page_h = landscape(A4)
    navy = (0.043, 0.129, 0.251)
    accent_soft = (0.906, 0.941, 0.980)

    c.setFillColorRGB(1, 1, 1)
    c.rect(0, 0, page_w, page_h, fill=1, stroke=0)

    # Decorative corner accents — soft blue arc top-right, navy arc bottom-left
    c.setFillColorRGB(*accent_soft)
    c.circle(page_w + 15 * mm, page_h + 5 * mm, 95 * mm, fill=1, stroke=0)
    c.setFillColorRGB(*navy)
    c.circle(-15 * mm, -10 * mm, 85 * mm, fill=1, stroke=0)

    if os.path.exists(LOGO_PATH):
        c.drawImage(LOGO_PATH, 18 * mm, page_h - 40 * mm, width=26 * mm, height=20 * mm,
                    preserveAspectRatio=True, anchor='c', mask='auto')

    c.setFillColorRGB(*navy)
    c.setFont('Helvetica-Bold', 34)
    c.drawCentredString(page_w / 2, page_h - 58 * mm, 'CERTIFICATE OF PARTICIPATION')

    c.setFillColorRGB(0.235, 0.322, 0.451)  # var(--body)
    c.setFont('Helvetica', 13)
    c.drawCentredString(page_w / 2, page_h - 78 * mm, 'This certificate is proudly presented to')

    c.setFillColorRGB(*navy)
    c.setFont('Times-BoldItalic', 30)
    name = fit_text(c, reg['name'], 'Times-BoldItalic', 30, page_w - 80 * mm)
    c.drawCentredString(page_w / 2, page_h - 95 * mm, name)
    name_w = c.stringWidth(name, 'Times-BoldItalic', 30)
    c.setLineWidth(0.8)
    c.setStrokeColorRGB(*navy)
    c.line(page_w / 2 - name_w / 2 - 6 * mm, page_h - 99 * mm, page_w / 2 + name_w / 2 + 6 * mm, page_h - 99 * mm)

    c.setFillColorRGB(0.235, 0.322, 0.451)
    c.setFont('Helvetica', 12.5)
    participated_as = (reg['participation_category'] or 'Attendee')
    c.drawCentredString(page_w / 2, page_h - 112 * mm,
                         'for participating in the Early Career Research Initiative Symposium 2026 (ECRI 2026)')
    c.drawCentredString(page_w / 2, page_h - 120 * mm,
                         f'held 22–23 October 2026 in Harare, Zimbabwe, as {participated_as}.')

    c.setFillColorRGB(*navy)
    c.setFont('Helvetica-Bold', 12)
    c.drawCentredString(page_w / 2, 42 * mm, 'Tatenda Makanga')
    c.setLineWidth(0.6)
    c.line(page_w / 2 - 32 * mm, 39 * mm, page_w / 2 + 32 * mm, 39 * mm)
    c.setFillColorRGB(0.361, 0.424, 0.541)
    c.setFont('Helvetica', 9)
    c.drawCentredString(page_w / 2, 34 * mm, 'ECRI 2026 Organising Committee')

    c.setFont('Helvetica-Oblique', 9.5)
    c.drawCentredString(page_w / 2, 16 * mm, 'CeSHHAR Zimbabwe · ECRI 2026 · 22–23 October 2026 · Harare')


def pdf_filename_for(name, suffix, code=None):
    """code is included when many of these files might land in the same place (a ZIP,
    a shared downloads folder) so two attendees with the same name can't collide."""
    safe = re.sub(r'[^A-Za-z0-9 _-]', '', name or 'attendee').strip().replace(' ', '-')
    if code:
        safe_code = re.sub(r'[^A-Za-z0-9-]', '', code)
        return f'{safe or "attendee"}-{safe_code}-{suffix}.pdf'
    return f'{safe or "attendee"}-{suffix}.pdf'


def make_badge_pdf_bytes(reg):
    badge_w, badge_h = 90 * mm, 50 * mm
    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=(badge_w, badge_h))
    draw_badge(c, 0, 0, badge_w, badge_h, reg)
    c.save()
    return buf.getvalue()


def make_certificate_pdf_bytes(reg):
    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=landscape(A4))
    draw_certificate(c, reg)
    c.save()
    return buf.getvalue()


@app.route('/ecri/api/admin/badges')
@require_role('admin')
def api_admin_badges():
    """Bulk download — a ZIP of each registrant's own individual badge PDF, not one
    combined multi-page document, so a file can actually be handed to one person."""
    db = get_db()
    rows = db.execute("SELECT * FROM registrations ORDER BY name").fetchall()

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for reg in rows:
            zf.writestr(pdf_filename_for(reg['name'], 'badge', reg['code']), make_badge_pdf_bytes(reg))
    zip_buf.seek(0)
    return app.response_class(
        zip_buf.getvalue(), mimetype='application/zip',
        headers={'Content-Disposition': 'attachment; filename=ecri-2026-badges.zip'},
    )


@app.route('/ecri/api/admin/badge/<reg_id>')
@require_role('admin')
def api_admin_single_badge(reg_id):
    db = get_db()
    reg = db.execute("SELECT * FROM registrations WHERE id = ?", (reg_id,)).fetchone()
    if not reg:
        return jsonify({'ok': False, 'error': 'Registration not found.'}), 404
    return app.response_class(
        make_badge_pdf_bytes(reg), mimetype='application/pdf',
        headers={'Content-Disposition': f'attachment; filename={pdf_filename_for(reg["name"], "badge")}'},
    )


@app.route('/ecri/api/admin/email-badges', methods=['POST'])
@require_role('admin')
def api_email_badges():
    db = get_db()
    rows = db.execute("SELECT * FROM registrations WHERE email != '' ORDER BY name").fetchall()
    items = []
    for reg in rows:
        body = (
            f"Hi {reg['name']},\n\nYour ECRI 2026 badge is attached — print it or show it on your "
            f"phone at check-in.\n\nCeSHHAR Zimbabwe · ECRI 2026"
        )
        items.append((reg['email'], make_badge_pdf_bytes(reg), pdf_filename_for(reg['name'], 'badge'), body))
    if not items:
        return jsonify({'ok': False, 'error': 'No registrations with an email on file.'}), 400
    threading.Thread(target=send_bulk_pdfs, args=(items, 'ECRI 2026 — your badge'), daemon=True).start()
    return jsonify({'ok': True, 'count': len(items)})


@app.route('/ecri/api/admin/check-in', methods=['POST'])
@require_role('admin')
def api_admin_check_in():
    data = request.json or {}
    code = (data.get('code') or '').strip().upper()
    db = get_db()
    row = db.execute("SELECT * FROM registrations WHERE code = ?", (code,)).fetchone()
    if not row:
        return jsonify({'ok': False, 'error': 'No registration found for that code.'}), 404
    if row['checked_in_at']:
        return jsonify({
            'ok': True, 'already': True, 'name': row['name'], 'institution': row['institution'],
            'checked_in_at': row['checked_in_at'],
        })
    now = datetime.datetime.utcnow().isoformat()
    db.execute("UPDATE registrations SET checked_in_at = ? WHERE code = ?", (now, code))
    db.commit()
    return jsonify({'ok': True, 'already': False, 'name': row['name'], 'institution': row['institution']})


@app.route('/ecri/api/admin/certificates')
@require_role('admin')
def api_admin_certificates():
    """Bulk download — a ZIP of each checked-in attendee's own individual certificate
    PDF (certificates are issued after check-in, not to everyone who merely registered)."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM registrations WHERE checked_in_at IS NOT NULL ORDER BY name"
    ).fetchall()
    if not rows:
        return jsonify({'ok': False, 'error': 'No checked-in attendees yet — certificates are issued after check-in.'}), 400

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for reg in rows:
            zf.writestr(pdf_filename_for(reg['name'], 'certificate', reg['code']), make_certificate_pdf_bytes(reg))
            db.execute(
                "UPDATE registrations SET certificate_issued_at = ? WHERE id = ?",
                (datetime.datetime.utcnow().isoformat(), reg['id']),
            )
    db.commit()
    zip_buf.seek(0)
    return app.response_class(
        zip_buf.getvalue(), mimetype='application/zip',
        headers={'Content-Disposition': 'attachment; filename=ecri-2026-certificates.zip'},
    )


@app.route('/ecri/api/admin/certificate/<reg_id>')
@require_role('admin')
def api_admin_single_certificate(reg_id):
    db = get_db()
    reg = db.execute("SELECT * FROM registrations WHERE id = ?", (reg_id,)).fetchone()
    if not reg:
        return jsonify({'ok': False, 'error': 'Registration not found.'}), 404
    if not reg['checked_in_at']:
        return jsonify({'ok': False, 'error': 'This person has not checked in yet.'}), 400

    pdf_bytes = make_certificate_pdf_bytes(reg)
    db.execute(
        "UPDATE registrations SET certificate_issued_at = ? WHERE id = ?",
        (datetime.datetime.utcnow().isoformat(), reg['id']),
    )
    db.commit()
    return app.response_class(
        pdf_bytes, mimetype='application/pdf',
        headers={'Content-Disposition': f'attachment; filename={pdf_filename_for(reg["name"], "certificate")}'},
    )


@app.route('/ecri/api/admin/email-certificates', methods=['POST'])
@require_role('admin')
def api_email_certificates():
    """Meant to be triggered after the event — emails every checked-in attendee their
    own certificate, once, rather than making each of them come back to the site."""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM registrations WHERE checked_in_at IS NOT NULL AND email != '' ORDER BY name"
    ).fetchall()
    items = []
    for reg in rows:
        body = (
            f"Hi {reg['name']},\n\nThank you for participating in ECRI 2026! Your certificate of "
            f"participation is attached.\n\nCeSHHAR Zimbabwe · ECRI 2026"
        )
        items.append((reg['email'], make_certificate_pdf_bytes(reg), pdf_filename_for(reg['name'], 'certificate'), body))
        db.execute(
            "UPDATE registrations SET certificate_issued_at = ? WHERE id = ?",
            (datetime.datetime.utcnow().isoformat(), reg['id']),
        )
    db.commit()
    if not items:
        return jsonify({'ok': False, 'error': 'No checked-in attendees with an email on file.'}), 400
    threading.Thread(target=send_bulk_pdfs, args=(items, 'ECRI 2026 — your certificate of participation'), daemon=True).start()
    return jsonify({'ok': True, 'count': len(items)})


@app.route('/ecri/api/my-certificate')
def api_my_certificate():
    """Self-service: a checked-in attendee downloads their own certificate directly,
    the same way they already access My schedule — no admin hand-off needed."""
    email = resolve_attendee_email(request.args.get('token'))
    if not email:
        return jsonify({'ok': False, 'error': 'Your link has expired. Request a new one from My schedule.'}), 403

    db = get_db()
    reg = db.execute(
        "SELECT * FROM registrations WHERE email = ? ORDER BY created_at DESC LIMIT 1", (email,)
    ).fetchone()
    if not reg:
        return jsonify({'ok': False, 'error': 'No registration found for this account.'}), 404
    if not reg['checked_in_at']:
        return jsonify({'ok': False, 'error': 'Certificates are available after check-in at the event.'}), 400

    pdf_bytes = make_certificate_pdf_bytes(reg)
    if not reg['certificate_issued_at']:
        db.execute(
            "UPDATE registrations SET certificate_issued_at = ? WHERE id = ?",
            (datetime.datetime.utcnow().isoformat(), reg['id']),
        )
        db.commit()
    return app.response_class(
        pdf_bytes, mimetype='application/pdf',
        headers={'Content-Disposition': f'attachment; filename={pdf_filename_for(reg["name"], "certificate")}'},
    )


def send_decision_email(abstract, status):
    """Authors get the outcome only — never the reviewers' comments (see
    feedback_ecri_review_decision_model). Accepted authors also get presentation
    type/instructions and a registration reminder, per the abstract spec's
    'Acceptance communication' section."""
    title, author, email, ref = abstract['title'], abstract['presenting_author'], abstract['email'], abstract['ref']
    if status in ('Accepted — oral', 'Accepted — poster'):
        presentation_type = 'an oral presentation' if status == 'Accepted — oral' else 'a poster'
        instructions = (
            'Oral presentations are allocated a 12-minute slot plus 3 minutes for questions; please prepare '
            'slides in 16:9 format.' if status == 'Accepted — oral' else
            'Posters should be portrait A0 size; printing support can be requested when you register.'
        )
        body = (
            f'Hi {author},\n\nCongratulations — your abstract "{title}" ({ref}) has been accepted for '
            f'ECRI 2026 as {presentation_type}.\n\n{instructions}\n\n'
            f'If you have not yet registered to attend, please do so here:\n'
            f'{PUBLIC_BASE}/register\n\n'
            f'The full programme, including your session time, will be shared closer to the event.\n\n'
            f'CeSHHAR Zimbabwe · ECRI 2026'
        )
        subject = f'ECRI 2026 — your abstract has been accepted ({ref})'
    elif status == 'Revise and resubmit':
        body = (
            f'Hi {author},\n\nThank you for submitting "{title}" ({ref}) to ECRI 2026. The committee would '
            f'like you to revise and resubmit your abstract. Please submit a revised version via Submit an '
            f'abstract before the deadline ({SUBMISSION_DEADLINE}):\n'
            f'{PUBLIC_BASE}/submit\n\n'
            f'CeSHHAR Zimbabwe · ECRI 2026'
        )
        subject = f'ECRI 2026 — please revise and resubmit your abstract ({ref})'
    elif status == 'Not accepted':
        body = (
            f'Hi {author},\n\nThank you for submitting "{title}" ({ref}) to ECRI 2026. After committee review, '
            f'we are unable to offer it a place in this year\'s programme. We received a strong field of '
            f'submissions and encourage you to submit again in future.\n\n'
            f'CeSHHAR Zimbabwe · ECRI 2026'
        )
        subject = f'ECRI 2026 — outcome of your abstract submission ({ref})'
    else:
        return  # 'Under review' — no email, that's the starting state, not a decision
    send_email(email, subject, body)


@app.route('/ecri/api/admin/decision', methods=['POST'])
@require_role('admin')
def api_admin_decision():
    data = request.json or {}
    ref = data.get('ref')
    status = data.get('status')
    if status not in DECISION_STATUSES:
        return jsonify({'ok': False, 'error': 'Unknown status.'}), 400
    db = get_db()
    abstract = db.execute("SELECT * FROM abstracts WHERE ref = ?", (ref,)).fetchone()
    if not abstract:
        return jsonify({'ok': False, 'error': 'Abstract not found.'}), 404
    status_changed = abstract['status'] != status
    db.execute("UPDATE abstracts SET status = ? WHERE ref = ?", (status, ref))
    if status not in ('Accepted — oral', 'Accepted — poster'):
        db.execute("DELETE FROM allocations WHERE abstract_ref = ?", (ref,))
    db.commit()
    if status_changed:
        send_decision_email(abstract, status)
    return jsonify({'ok': True})


@app.route('/ecri/programme')
def programme_page():
    db = get_db()
    token = request.args.get('token') or ''
    email = resolve_attendee_email(token)

    starred_ids = set()
    if email:
        starred_ids = {
            r['session_id'] for r in db.execute(
                "SELECT session_id FROM session_stars WHERE email = ?", (email,)
            ).fetchall()
        }

    sessions_by_day = {1: [], 2: []}
    rows = db.execute(
        "SELECT s.*, a.photo AS presenter_photo, a.body AS abstract_body, "
        "a.presenting_author AS abstract_author, a.institution AS abstract_institution "
        "FROM sessions s LEFT JOIN abstracts a ON a.ref = s.abstract_ref "
        "ORDER BY s.day, s.time"
    ).fetchall()
    for s in rows:
        sessions_by_day.setdefault(s['day'], []).append(s)

    return render_template(
        'programme.html',
        sessions_by_day=sessions_by_day,
        theme_style=THEME_STYLE,
        token=token,
        signed_in=bool(email),
        starred_ids=starred_ids,
    )


@app.route('/ecri/api/admin/add-session', methods=['POST'])
@require_role('admin')
def api_admin_add_session():
    data = request.json or {}
    day = data.get('day')
    time_ = normalize_session_time(data.get('time') or '')
    title = (data.get('title') or '').strip()
    who = (data.get('who') or '').strip()
    room = (data.get('room') or '').strip()
    theme = (data.get('theme') or '').strip()
    abstract_ref = (data.get('abstract_ref') or '').strip() or None

    if day not in (1, 2) or not time_ or not title or theme not in SESSION_THEMES:
        return jsonify({'ok': False, 'error': 'Day, time, title and theme are required.'}), 400

    db = get_db()
    db.execute(
        "INSERT INTO sessions (id, day, time, title, who, room, theme, abstract_ref, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (os.urandom(8).hex(), day, time_, title, who, room, theme, abstract_ref,
         datetime.datetime.utcnow().isoformat()),
    )
    db.commit()
    return jsonify({'ok': True})


@app.route('/ecri/api/admin/delete-session', methods=['POST'])
@require_role('admin')
def api_admin_delete_session():
    data = request.json or {}
    session_id = data.get('id')
    db = get_db()
    db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/ecri/api/admin/announce', methods=['POST'])
@require_role('admin')
def api_admin_announce():
    data = request.json or {}
    headline = (data.get('headline') or '').strip()
    body = (data.get('body') or '').strip()
    priority = data.get('priority') or 'Standard'
    also_email = bool(data.get('also_email'))
    if not headline:
        return jsonify({'ok': False, 'error': 'Headline is required.'}), 400
    db = get_db()
    db.execute(
        "INSERT INTO announcements (id, headline, body, priority, sent_at) VALUES (?, ?, ?, ?, ?)",
        (os.urandom(8).hex(), headline, body, priority, datetime.datetime.utcnow().isoformat()),
    )
    db.commit()

    emailed_count = 0
    if also_email:
        recipients = [
            r['email'] for r in db.execute("SELECT DISTINCT email FROM registrations").fetchall()
        ]
        emailed_count = len(recipients)
        if recipients:
            email_body = f'{body}\n\nCeSHHAR Zimbabwe · ECRI 2026' if body else 'CeSHHAR Zimbabwe · ECRI 2026'
            threading.Thread(
                target=send_bulk_email,
                args=(recipients, f'ECRI 2026 — {headline}', email_body),
                daemon=True,
            ).start()

    return jsonify({'ok': True, 'emailed_count': emailed_count})


@app.route('/ecri/api/admin/set-tracks', methods=['POST'])
@require_role('admin')
def api_admin_set_tracks():
    data = request.json or {}
    email = (data.get('email') or '').strip().lower()
    tracks = [t for t in (data.get('tracks') or []) if t in THEMES]

    db = get_db()
    existing = db.execute("SELECT id FROM staff_users WHERE email = ?", (email,)).fetchone()
    if not existing:
        return jsonify({'ok': False, 'error': 'Account not found.'}), 404

    db.execute("UPDATE staff_users SET tracks = ? WHERE email = ?", ('|'.join(tracks), email))
    db.commit()
    return jsonify({'ok': True})


@app.route('/ecri/api/admin/add-staff', methods=['POST'])
@require_role('admin')
def api_admin_add_staff():
    data = request.json or {}
    name = (data.get('name') or '').strip()
    email = (data.get('email') or '').strip().lower()
    roles = data.get('roles') or []
    roles = [r for r in roles if r in ('reviewer', 'chair', 'admin')]
    tracks = [t for t in (data.get('tracks') or []) if t in THEMES]

    if not name or not email or not EMAIL_RE.match(email) or not roles:
        return jsonify({'ok': False, 'error': 'Name, valid email and at least one role are required.'}), 400

    db = get_db()
    existing = db.execute("SELECT id FROM staff_users WHERE email = ?", (email,)).fetchone()
    if existing:
        # Update name/roles/tracks only — never touch an existing password from here.
        db.execute("UPDATE staff_users SET name=?, roles=?, tracks=? WHERE email=?",
                   (name, ','.join(roles), '|'.join(tracks), email))
    else:
        # Unusable placeholder hash: login is impossible until they set a real
        # password via the emailed link, so there's never a shared/known password.
        placeholder_hash = generate_password_hash(os.urandom(16).hex())
        db.execute(
            "INSERT INTO staff_users (id, email, password_hash, name, roles, tracks, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (os.urandom(8).hex(), email, placeholder_hash, name, ','.join(roles), '|'.join(tracks),
             datetime.datetime.utcnow().isoformat()),
        )
    db.commit()

    link = f'{PUBLIC_BASE}/staff/set-password?token={make_staff_token(email)}'
    send_email(
        email,
        'ECRI 2026 — set up your staff account',
        f'Hi {name},\n\n'
        f'You have been added as {" / ".join(roles)} on the ECRI 2026 platform.\n'
        f'Set your password here to activate your account:\n{link}\n'
        f'(valid for 48 hours)\n\n'
        f'CeSHHAR Zimbabwe · ECRI 2026',
    )
    return jsonify({'ok': True})


@app.route('/ecri/staff/set-password', methods=['GET'])
def staff_set_password_page():
    email = verify_staff_token(request.args.get('token'))
    return render_template('staff_set_password.html', valid=bool(email), token=request.args.get('token') or '')


@app.route('/ecri/api/staff/set-password', methods=['POST'])
def api_staff_set_password():
    data = request.json or {}
    email = verify_staff_token(data.get('token'))
    password = data.get('password') or ''
    if not email:
        return jsonify({'ok': False, 'error': 'This link has expired. Request a new one.'}), 403
    if len(password) < 8:
        return jsonify({'ok': False, 'error': 'Password must be at least 8 characters.'}), 400

    db = get_db()
    row = db.execute("SELECT id, roles FROM staff_users WHERE email = ?", (email,)).fetchone()
    if not row:
        return jsonify({'ok': False, 'error': 'Account not found.'}), 404

    db.execute("UPDATE staff_users SET password_hash = ? WHERE email = ?",
               (generate_password_hash(password), email))
    db.commit()
    session['staff_email'] = email
    landing = staff_default_landing((row['roles'] or '').split(','))
    return jsonify({'ok': True, 'redirect': landing})


@app.route('/ecri/staff/forgot-password', methods=['GET'])
def staff_forgot_password_page():
    return render_template('staff_forgot_password.html')


@app.route('/ecri/api/staff/forgot-password', methods=['POST'])
def api_staff_forgot_password():
    data = request.json or {}
    email = (data.get('email') or '').strip().lower()
    if email:
        db = get_db()
        row = db.execute("SELECT * FROM staff_users WHERE email = ?", (email,)).fetchone()
        if row:
            link = f'{PUBLIC_BASE}/staff/set-password?token={make_staff_token(email)}'
            send_email(
                email,
                'ECRI 2026 — reset your staff password',
                f'Use this link to set a new password:\n{link}\n(valid for 48 hours)\n\n'
                f'CeSHHAR Zimbabwe · ECRI 2026',
            )
    # Always ok, regardless of whether the account exists — no enumeration.
    return jsonify({'ok': True})


def send_due_reminders():
    """Runs once per minute in a background thread. Emails anyone who starred a session
    starting in the next REMINDER_MINUTES_BEFORE minutes, exactly once per (email, session)
    — marks reminded_at regardless of whether an email was actually sent, so a session
    missed while the app was down never gets a late/stale reminder."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        now = datetime.datetime.now()
        due = db.execute(
            "SELECT st.email, s.* FROM session_stars st "
            "JOIN sessions s ON s.id = st.session_id "
            "WHERE st.reminded_at IS NULL"
        ).fetchall()
        for row in due:
            event_date = EVENT_DATES.get(row['day'])
            if not event_date:
                continue
            try:
                hour, minute = (int(x) for x in row['time'].split(':'))
            except ValueError:
                continue
            session_dt = datetime.datetime.combine(event_date, datetime.time(hour, minute))
            remind_at = session_dt - datetime.timedelta(minutes=REMINDER_MINUTES_BEFORE)

            if now < remind_at:
                continue  # not due yet

            if now < session_dt:
                send_email(
                    row['email'],
                    f"ECRI 2026 — starting soon: {row['title']}",
                    f"\"{row['title']}\" starts in about {REMINDER_MINUTES_BEFORE} minutes "
                    f"({row['time']}{', ' + row['room'] if row['room'] else ''}).\n\n"
                    f"CeSHHAR Zimbabwe · ECRI 2026",
                )
            # else: window already passed (app was down, or ran late) — mark handled, don't send.

            db.execute(
                "UPDATE session_stars SET reminded_at = ? WHERE email = ? AND session_id = ?",
                (now.isoformat(), row['email'], row['id']),
            )
            db.commit()
    except Exception:
        app.logger.exception('Reminder check failed')
    finally:
        db.close()


def reminder_loop():
    while True:
        send_due_reminders()
        time_module.sleep(60)


# Poster judging (self-contained blueprint, own SQLite db in ~/ECRI_Poster_Judging)
import sys
sys.path.insert(0, os.path.join(os.path.dirname(BASE_DIR), "ECRI_Poster_Judging"))
os.environ.setdefault('POSTER_BASE_URL', PUBLIC_BASE + '/judging')
from poster_judging import bp as poster_bp
app.register_blueprint(poster_bp, url_prefix="/ecri/judging")


if __name__ == '__main__':
    init_db()
    threading.Thread(target=reminder_loop, daemon=True).start()
    app.run(host='127.0.0.1', port=5010)
