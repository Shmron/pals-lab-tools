"""
Named pregnancy-relative exposure windows, matching the definitions already
validated and used across this team's own HE2AT exposure-linkage pipeline
(Phase3_Exposure_Linkage.ipynb) — so a PRECISE/DYAD/HE2AT researcher gets the
same windows they already know, instead of reinventing a date-range picker
that means nothing without a global calendar range per participant.

Day 0 = conception for preconception/trimester/full-pregnancy windows.
`day_to=None` means "open-ended, capped at that participant's own delivery
date" rather than a fixed offset.
"""

PREGNANCY_WINDOWS = {
    "precon90": {
        "label": "Preconception (3 months)",
        "description": "90 days before conception",
        "day_from": -90, "day_to": 0,
    },
    "precon30": {
        "label": "Preconception (1 month)",
        "description": "30 days before conception",
        "day_from": -30, "day_to": 0,
    },
    "T1": {
        "label": "First trimester",
        "description": "Gestational days 1-90 (weeks 1-13)",
        "day_from": 1, "day_to": 90,
    },
    "T2": {
        "label": "Second trimester",
        "description": "Gestational days 91-181 (weeks 14-26)",
        "day_from": 91, "day_to": 181,
    },
    "T3": {
        "label": "Third trimester",
        "description": "Day 182 to delivery (week 27+)",
        "day_from": 182, "day_to": None,
    },
    "peripartum": {
        "label": "Peripartum",
        "description": "Final 28 days before delivery",
        "day_from": None, "day_to": None, "peripartum": True,
    },
    "full": {
        "label": "Full pregnancy",
        "description": "All days from conception to delivery",
        "day_from": 0, "day_to": None,
    },
}


def compute_window_dates(conception_date, delivery_date, window_key):
    """
    conception_date, delivery_date: pandas.Timestamp or None/NaT.
    Returns (start_date, end_date) as pandas.Timestamp, or None if the
    inputs required for this window aren't available for this row.
    """
    import pandas as pd

    win = PREGNANCY_WINDOWS.get(window_key)
    if win is None:
        return None
    if conception_date is None or pd.isna(conception_date):
        return None

    if win.get("peripartum"):
        if delivery_date is None or pd.isna(delivery_date):
            return None
        return delivery_date - pd.Timedelta(days=28), delivery_date

    start = conception_date + pd.Timedelta(days=win["day_from"])
    if win["day_to"] is None:
        if delivery_date is None or pd.isna(delivery_date):
            return None
        end = delivery_date
    else:
        end = conception_date + pd.Timedelta(days=win["day_to"])
    if end < start:
        return None
    return start, end


def compute_custom_window_dates(index_date, day_from, day_to):
    """
    General-purpose version of compute_window_dates for a study that isn't
    pregnancy-shaped — any single per-row index/event date (diagnosis,
    admission, symptom onset…) plus an explicit day_from/day_to lag span
    relative to it. No delivery-date fallback: both ends are always
    concrete offsets, so there's nothing open-ended to resolve.

    index_date: pandas.Timestamp or None/NaT.
    day_from, day_to: ints, signed days relative to index_date (day_from
    may be negative for "before the index date").
    Returns (start_date, end_date) as pandas.Timestamp, or None.
    """
    import pandas as pd

    if index_date is None or pd.isna(index_date):
        return None
    start = index_date + pd.Timedelta(days=day_from)
    end = index_date + pd.Timedelta(days=day_to)
    if end < start:
        return None
    return start, end


def build_case_crossover_referents(index_date, max_referents=8):
    """
    Time-stratified case-crossover referent scheme (Levy/Bateson & Schwartz):
    referent days are every other day sharing the index day's weekday, within
    the same calendar month as the index date, excluding the index date
    itself. This is the standard design for self-matched environmental
    exposure studies — it controls for day-of-week and seasonal/monthly
    confounders by construction, without the researcher having to supply or
    choose control days themselves.

    index_date: pandas.Timestamp or None/NaT.
    Returns a list of pandas.Timestamp (typically 3-4 for a 28-31 day month),
    capped at max_referents as a sanity bound. Empty list if index_date is
    missing.
    """
    import pandas as pd

    if index_date is None or pd.isna(index_date):
        return []
    month_start = index_date.replace(day=1)
    next_month_start = month_start + pd.DateOffset(months=1)
    month_days = pd.date_range(month_start, next_month_start - pd.Timedelta(days=1), freq='D')
    referents = [d for d in month_days if d.weekday() == index_date.weekday() and d != index_date]
    return referents[:max_referents]
