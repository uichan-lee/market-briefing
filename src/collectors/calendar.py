"""Macro event and options-expiration dates. SPEC §2.2④, partial.

Full design and source verification: ``notes/calendar-collector-plan.md``.
Builds two of SPEC §2.2④'s four sub-sources — US macro release dates
(CPI, Employment Situation, FOMC) and standard monthly options expiration.
US individual-company earnings dates and KR ex-dividend/IPO dates stay
absent, named as such in ``src/report/render.py``'s ``render_calendar``.

**Module name checked, not assumed.** This project only ever runs as
``python -m src.collectors.calendar`` / ``uv run python -m ...`` from the
repo root (``pythonpath = ["."]`` in ``pyproject.toml``), under which
``import calendar`` inside this file resolves to the *stdlib* module, not
this one — Python's absolute-import model makes this file's own identity
``src.collectors.calendar``, distinct from bare ``calendar``. Confirmed live
with a throwaway file during planning. The hazard would be real only if
someone ``cd``'d into ``src/collectors/`` and ran ``python calendar.py``
directly, which nothing in this project's tests, CI, or ``collect_daily.py``
ever does.

**Two FRED traps, one documented and one not.** ``fred/release/dates``
defaults ``include_release_dates_with_no_data`` to ``false``, which FRED's
own docs say excludes future release dates — verified live (CPI: 1 row
without the flag, 5 with it, including 4 future). Separately,
``release_id=101`` ("FOMC Press Release") does **not** return meeting dates
at all — it is a *daily* publication series, verified live to return one row
per calendar day over a test window. Using it would produce an "FOMC today"
flag firing every day, silently, with no error — the same failure shape
CLAUDE.md's warning about substituting pykrx's trailing EPS for ``rev_4w``
describes. FOMC dates come from ``federalreserve.gov`` instead; see below.

**FOMC meetings can span two days and two months, and this schema says so.**
``date_start`` is the meeting's first day; ``date`` is its final day (when
the statement/decision lands) and is what "same-day/next-day" comparisons in
the renderer use. For CPI, Employment Situation, and options expiration —
all single-day events — the two columns are equal.

**``known_at_utc`` is fetch time, not observation time — the inverse of
``macro.py``'s rule, and deliberately so.** ``macro.py`` computes it as the
day after a FRED value is observed, because that value is unknown until
published after the fact. A CPI/FOMC/options-expiry date is the opposite: it
is announced months ahead of the event itself, so its content is fully
knowable at fetch time. Every row from one ``fetch()`` call therefore shares
one ``known_at_utc`` — the instant that call ran — and ``fetch()`` takes no
``as_of`` parameter, unlike ``macro.py``: with one shared timestamp per call
a per-row filter would be all-or-nothing, not selective, so it would not
mean anything. This is a documented deviation from the existing pattern in
service of the same look-ahead-prohibition intent, not an exception to it.

**Two more documented deviations from the standard four-check template**
(the third and fourth are on top of the two above):
``check_trading_day_continuity`` does not apply — there is no daily exchange
calendar an event date is measured against — so check three is
:func:`check_event_continuity`, a maximum-inter-occurrence-gap check per
event type. ``validate.check_known_value`` assumes a numeric column
(``float(matched[column].iloc[0])`` internally) and breaks on a date, so
check four is the local :func:`check_known_date` instead of the shared
helper.

**Options expiration is not naive "third Friday" arithmetic.** When that
Friday is a US market holiday, expiration moves to the *preceding* trading
day, never forward (OCC rule). Computed against
``src.util.session.trading_days``/``is_trading_day``, not a second holiday
list, so it never drifts from the one calendar this project already treats
as authoritative. Verified live: June 2026 expiration is Thursday
2026-06-18, not Friday 2026-06-19, because 2026-06-19 is Juneteenth.

**No watchlist dependency, confirmed rather than assumed.** Macro events and
computed options expiry are watchlist-independent, exactly like
``macro.py``'s six FRED series — neither imports ``load_watchlist``.

**No ``main()``/CLI.** ``kr_news.py`` is the only collector with one, because
a separate hourly Actions workflow calls it directly outside
``collect_daily.py``'s morning/evening cadence. This collector has no such
standalone cadence requirement.
"""

from __future__ import annotations

import calendar as _stdlib_calendar
import datetime as dt
import os
import re
from html.parser import HTMLParser

import pandas as pd
import requests

from src.collectors.validate import (
    CheckResult,
    ValidationReport,
    check_missing_ratio,
    check_schema,
    validate,
)
from src.util.session import is_trading_day, now_utc, previous_trading_day

COLLECTOR = "calendar"

_FRED_API = "https://api.stlouisfed.org/fred/release/dates"
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

# FRED release IDs, confirmed against the live API on 2026-08-14 with
# `include_release_dates_with_no_data=true` and read back rather than
# recalled. release_id=101 ("FOMC Press Release") deliberately does not
# appear here — see the module docstring.
FRED_RELEASES: dict[str, int] = {
    "cpi": 10,
    "employment_situation": 50,
}

EVENTS = ("cpi", "employment_situation", "fomc", "options_expiration_monthly")

# Single-month cells on the Fed's page use the full name ("September"); a
# month-spanning cell uses the 3-letter abbreviation for both sides
# ("Apr/May", "Jan/Feb", "Oct/Nov") — confirmed against the live page
# (2026-08-14 fetch), not assumed. Both forms are accepted.
_MONTH_NUMBER: dict[str, int] = {
    name: i for i, name in enumerate(_stdlib_calendar.month_name) if name
} | {abbr: i for i, abbr in enumerate(_stdlib_calendar.month_abbr) if abbr}

SCHEMA = {
    "date": "datetime64[s]",
    "date_start": "datetime64[s]",
    "event": "object",
    "label": "object",
    "source": "object",
    "has_sep": "bool",
    "known_at_utc": "datetime64[ns, UTC]",
}

MISSING_THRESHOLDS = {"date": 0.0, "date_start": 0.0, "event": 0.0, "label": 0.0, "source": 0.0}

# Maximum plausible gap between consecutive occurrences of the same event,
# in days. cpi/employment_situation are monthly with holiday slack; fomc has
# 8 scheduled meetings/year (longest real gap ~7 weeks) plus occasional extra
# notation-vote entries, which only shorten gaps, never lengthen them;
# options_expiration_monthly is exactly monthly by construction. A gap wider
# than this means the fetch lost rows, not that the calendar is sparse.
MAX_GAP_DAYS = {
    "cpi": 45,
    "employment_situation": 40,
    "fomc": 60,
    "options_expiration_monthly": 35,
}

# Verified live 2026-08-14, from federalreserve.gov's own 2026 panel
# (id="42828"), September row: month "September", date "15-16*" — the
# primary-source page itself, not a second reader of the same feed.
KNOWN_VALUES: list[dict[str, object]] = [
    {
        "where": {"event": "fomc", "date": dt.date(2026, 9, 16)},
        "column": "date_start",
        "expected": dt.date(2026, 9, 15),
    },
    # Cross-checked directly against this repo's own trading_days("US", ...):
    # 2026-06-18 is returned, 2026-06-19 (Juneteenth) is correctly excluded.
    # The strongest check available here — it exercises the same
    # session-calendar dependency the expiry computation itself uses, the
    # same principle behind macro.py's SPY check reading an independent
    # vendor rather than the one it fetched from.
    {
        "where": {"event": "options_expiration_monthly", "date": dt.date(2026, 6, 18)},
        "column": "date",
        "expected": dt.date(2026, 6, 18),
    },
]


class CalendarError(RuntimeError):
    """A required credential or source was unavailable."""


# --- validation ------------------------------------------------------------


def check_event_continuity(
    df: pd.DataFrame, events: list[str], start: dt.date, end: dt.date
) -> CheckResult:
    """Check three's analog for a periodic event series, not a daily one.

    There is no exchange calendar an event date can be measured against —
    the trading-day-continuity check in :mod:`src.collectors.validate` is
    meaningless here. What is checkable is each event type's own maximum
    plausible spacing (:data:`MAX_GAP_DAYS`): a gap wider than that means
    rows were lost, not that the calendar is legitimately sparse.
    """
    problems: list[str] = []
    details: list[str] = []
    window_days = (end - start).days

    for event in events:
        max_gap = MAX_GAP_DAYS[event]
        subset = df[df["event"] == event] if "event" in df.columns else df.iloc[0:0]
        dates = sorted({d.date() for d in pd.to_datetime(subset["date"])})

        if not dates:
            if window_days >= max_gap:
                problems.append(
                    f"{event}: no rows in a {window_days}-day window (max gap {max_gap}d)"
                )
            continue

        if dates[0] - start > dt.timedelta(days=max_gap):
            problems.append(
                f"{event}: first row {dates[0]} is >{max_gap}d after window start {start}"
            )
        if end - dates[-1] > dt.timedelta(days=max_gap):
            problems.append(f"{event}: last row {dates[-1]} is >{max_gap}d before window end {end}")

        gaps = [b - a for a, b in zip(dates, dates[1:], strict=False)]
        widest = max(gaps, default=dt.timedelta(0))
        if widest > dt.timedelta(days=max_gap):
            problems.append(f"{event}: interior gap of {widest.days}d (max allowed {max_gap}d)")

        duplicated = subset["date"].duplicated().sum() if len(subset) else 0
        if duplicated:
            problems.append(f"{event}: {duplicated} duplicated dates")

        weekend = [d for d in dates if d.weekday() >= 5]
        if weekend:
            problems.append(f"{event}: {len(weekend)} rows on weekends, e.g. {weekend[0]}")

        # options_expiration_monthly is Friday by construction, except a
        # holiday-shifted month, which lands on the preceding trading day —
        # Thursday for a single-day US holiday, the only case observed or
        # expected. employment_situation is NOT reliably Friday, despite
        # "first Friday of the month" being the usual pattern: verified
        # against real 2026 FRED data that 2026-02-11 is a Wednesday and
        # 2026-07-02 is a Thursday, real BLS scheduling exceptions, not
        # collector defects. Asserting Friday there would fail correct data.
        if event == "options_expiration_monthly":
            wrong_day = [d for d in dates if d.weekday() not in (3, 4)]
            if wrong_day:
                problems.append(
                    f"{event}: {len(wrong_day)} rows not on Thu/Fri, e.g. {wrong_day[0]}"
                )

        details.append(f"{event} {len(dates)} rows")

    if problems:
        return CheckResult("event_continuity", False, "; ".join(problems))
    return CheckResult("event_continuity", True, ", ".join(details))


def check_known_date(
    df: pd.DataFrame, where: dict[str, object], column: str, expected: dt.date
) -> CheckResult:
    """:func:`src.collectors.validate.check_known_value`'s date-typed sibling.

    That function does ``float(matched[column].iloc[0])`` internally, which
    raises or misbehaves against a ``datetime64`` value. This collector's
    known facts are dates, so it gets its own comparator rather than coercing
    dates into epoch-floats to fit a signature built for prices.
    """
    label = ", ".join(f"{k}={v!r}" for k, v in where.items())

    if column not in df.columns:
        return CheckResult("known_value", False, f"{column} absent")

    mask = pd.Series(True, index=df.index)
    for key, value in where.items():
        if key not in df.columns:
            return CheckResult("known_value", False, f"selector column {key} absent")
        series = df[key]
        if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
            series = pd.to_datetime(series).dt.date
        mask &= series == value

    matched = df[mask]
    if len(matched) != 1:
        return CheckResult(
            "known_value", False, f"selector ({label}) matched {len(matched)} rows, expected 1"
        )

    actual = pd.Timestamp(matched[column].iloc[0]).date()
    if actual != expected:
        return CheckResult(
            "known_value", False, f"{column} at ({label}) is {actual}, expected {expected}"
        )
    return CheckResult("known_value", True, f"{column} at ({label}) == {actual}")


def validate_frame(
    df: pd.DataFrame,
    events: list[str],
    start: dt.date,
    end: dt.date,
    *,
    known_value: bool = True,
) -> ValidationReport:
    """Run all four checks (two of them collector-local) against a frame."""
    checks = [
        check_schema(df, SCHEMA),
        check_missing_ratio(df, MISSING_THRESHOLDS),
        check_event_continuity(df, events, start, end),
    ]
    if known_value:
        checks.extend(check_known_date(df, **kv) for kv in KNOWN_VALUES)
    return validate(COLLECTOR, checks)


# --- FOMC: federalreserve.gov, not FRED -------------------------------------


class _FomcHtmlParser(HTMLParser):
    """Extracts (year, month text, date text) triples from the Fed's calendar page.

    A stack-based capture rather than raw regex on tags: the live page's
    markup is not uniform enough for that to be safe — some rows carry an
    extra ``fomc-meeting--shaded`` class token before the class this parser
    matches on, and notation-vote rows sit at different nesting than regular
    2-day rows, while month cells nest their text inside a ``<strong>`` tag
    that date cells do not. Matching on "does this tag's class attribute
    *contain* the target substring" and capturing until the matching close
    tolerates both variations; regex is then only applied to the already-
    extracted text content, which is the narrow, safe use of it.
    """

    _YEAR_RE = re.compile(r"(\d{4}) FOMC Meetings")

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[int, str, str]] = []
        self._year: int | None = None
        self._stack: list[str] = []
        self._buffer: dict[str, str] = {"month": "", "date": "", "year_anchor": ""}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        classes = attr_map.get("class") or ""
        kind = ""
        if tag == "a" and attr_map.get("id"):
            kind = "year_anchor"
        elif "fomc-meeting__month" in classes:
            kind = "month"
        elif "fomc-meeting__date" in classes:
            kind = "date"
        self._stack.append(kind)
        if kind:
            self._buffer[kind] = ""

    def handle_data(self, data: str) -> None:
        for kind in ("month", "date", "year_anchor"):
            if kind in self._stack:
                self._buffer[kind] += data

    def handle_endtag(self, tag: str) -> None:
        if not self._stack:
            return
        kind = self._stack.pop()
        if kind == "year_anchor":
            match = self._YEAR_RE.search(self._buffer["year_anchor"])
            if match:
                self._year = int(match.group(1))
        elif kind == "date" and self._year is not None:
            month_text = self._buffer["month"].strip()
            date_text = self._buffer["date"].strip()
            if month_text and date_text:
                self.rows.append((self._year, month_text, date_text))


_NOTATION_RE = re.compile(r"^(\d+)\s*\(([^)]+)\)$")


def _parse_fomc_row(year: int, month_text: str, date_text: str) -> dict[str, object]:
    """Turn one (year, month cell, date cell) triple into a schema-shaped row.

    Handles, all confirmed present in the live page (2026-08-14 fetch):
    single-day 2-month meetings ("27-28" under "January"), SEP meetings
    ("17-18*"), month-spanning/rollover meetings ("30-1" under "Apr/May",
    "31-1" under "Jan/Feb" or "Oct/Nov" — the first day belongs to the first
    named month, the second day to the second, read off the month cell
    rather than inferred from day-number comparison, since "1 < 30" is not
    the reason two months are named), and notation-vote entries
    ("22 (notation vote)", not a 2-day meeting).
    """
    has_sep = date_text.endswith("*")
    date_text = date_text.rstrip("*").strip()

    months = [m.strip() for m in month_text.split("/")]
    month_start = _MONTH_NUMBER[months[0]]
    month_end = _MONTH_NUMBER[months[-1]]
    year_start = year
    # Only relevant for a hypothetical Dec/Jan pair, not observed in the live
    # 2021-2027 range this collector was verified against, but handled
    # explicitly rather than left to accidentally work.
    year_end = year + 1 if month_end < month_start else year

    notation = _NOTATION_RE.match(date_text)
    if notation:
        day = int(notation.group(1))
        note = notation.group(2)
        day_date = dt.date(year_start, month_start, day)
        return {
            "date_start": day_date,
            "date": day_date,
            "event": "fomc",
            "label": f"FOMC {note} ({day_date:%m/%d})",
            "source": FOMC_URL,
            "has_sep": has_sep,
        }

    day_start_text, _, day_end_text = date_text.partition("-")
    date_start = dt.date(year_start, month_start, int(day_start_text))
    date_end = dt.date(year_end, month_end, int(day_end_text))
    label = f"FOMC 회의 ({date_start:%m/%d}~{date_end:%m/%d})"
    if has_sep:
        label += " (경제전망요약)"
    return {
        "date_start": date_start,
        "date": date_end,
        "event": "fomc",
        "label": label,
        "source": FOMC_URL,
        "has_sep": has_sep,
    }


def _fetch_fomc_rows(
    *, html: str | None = None, timeout: int = 30
) -> tuple[list[dict], str | None]:
    """Fetch (or accept pre-fetched) FOMC meeting rows.

    ``html`` exists for tests, which pass the committed fixture rather than
    hitting the live page. Returns ``(rows, error)`` rather than raising, so
    a bad fetch here does not cost the CPI/employment_situation/options rows.
    """
    if html is None:
        try:
            response = requests.get(FOMC_URL, timeout=timeout)
        except requests.RequestException as exc:
            return [], f"fomc: {type(exc).__name__}: {exc}"
        if response.status_code != 200:
            return [], f"fomc: HTTP {response.status_code}"
        html = response.text

    parser = _FomcHtmlParser()
    parser.feed(html)
    rows = [_parse_fomc_row(year, month, date) for year, month, date in parser.rows]
    return rows, None


# --- CPI / Employment Situation: FRED ---------------------------------------


def _fetch_fred_dates(
    release_id: int, event: str, start: dt.date, api_key: str, *, timeout: int = 30
) -> tuple[list[dict], str | None]:
    """One FRED release's future-and-recent dates. Returns (rows, error).

    ``include_release_dates_with_no_data=true`` is required, not optional —
    without it FRED silently drops every future date (its own docs state
    this), which would make the calendar section always empty in production
    while every test that only checks past dates kept passing.
    """
    try:
        response = requests.get(
            _FRED_API,
            params={
                "release_id": release_id,
                "api_key": api_key,
                "file_type": "json",
                "realtime_start": start.isoformat(),
                "realtime_end": "9999-12-31",
                "include_release_dates_with_no_data": "true",
                "sort_order": "asc",
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return [], f"{event}: {type(exc).__name__}: {exc}"
    if response.status_code != 200:
        return [], f"{event}: HTTP {response.status_code}"

    payload = response.json().get("release_dates")
    if payload is None:
        return [], f"{event}: no release_dates in response"

    label = "CPI 발표" if event == "cpi" else "고용지표 발표"
    rows = [
        {
            "date_start": dt.date.fromisoformat(item["date"]),
            "date": dt.date.fromisoformat(item["date"]),
            "event": event,
            "label": label,
            "source": f"FRED release_id={release_id}",
            "has_sep": False,
        }
        for item in payload
    ]
    return rows, None


# --- options expiration: computed, no vendor --------------------------------


def _monthly_options_expiration(year: int, month: int) -> dt.date:
    """Third Friday of ``(year, month)``, rolled back if that Friday is a
    US market holiday (OCC rule — moves to the preceding trading day, never
    forward). Computed against this repo's own US trading calendar, not a
    second holiday list, so it never drifts from what the rest of the
    project treats as authoritative.
    """
    fridays = [
        day
        for day in _stdlib_calendar.Calendar().itermonthdates(year, month)
        if day.month == month and day.weekday() == 4
    ]
    third_friday = fridays[2]
    if is_trading_day("US", third_friday):
        return third_friday
    return previous_trading_day("US", third_friday)


def _options_expiration_rows(start: dt.date, end: dt.date) -> list[dict]:
    rows = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        expiry = _monthly_options_expiration(year, month)
        rows.append(
            {
                "date_start": expiry,
                "date": expiry,
                "event": "options_expiration_monthly",
                "label": f"{month}월 옵션 만기",
                "source": "computed:3rd-friday",
                "has_sep": False,
            }
        )
        month += 1
        if month > 12:
            month = 1
            year += 1
    return rows


# --- top-level fetch ---------------------------------------------------------


def fetch(
    start: dt.date,
    end: dt.date,
    *,
    api_key: str | None = None,
    fomc_html: str | None = None,
    now: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, ValidationReport]:
    """Fetch calendar events over ``[start, end]``.

    Every row gets the same ``known_at_utc`` — the instant this call ran —
    because every source here announces its dates well in advance of the
    event itself; see the module docstring for why that makes an
    ``as_of``-style per-row filter meaningless here, unlike ``macro.py``.

    Returns the frame and its report rather than raising on one bad source,
    so a FRED outage does not cost the FOMC/options rows or vice versa.
    """
    key = api_key or os.environ.get("FRED_API_KEY")
    if not key:
        raise CalendarError("FRED_API_KEY is not set")

    known_at = now or now_utc()
    failures: list[str] = []
    rows: list[dict] = []

    for event, release_id in FRED_RELEASES.items():
        fetched, error = _fetch_fred_dates(release_id, event, start, key)
        if error:
            failures.append(error)
        rows.extend(fetched)

    fomc_rows, error = _fetch_fomc_rows(html=fomc_html)
    if error:
        failures.append(error)
    rows.extend(fomc_rows)

    rows.extend(_options_expiration_rows(start, end))

    if rows:
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"]).astype("datetime64[s]")
        df["date_start"] = pd.to_datetime(df["date_start"]).astype("datetime64[s]")
        df = df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))]
        df = df.astype({k: v for k, v in SCHEMA.items() if k != "known_at_utc"})
        df["known_at_utc"] = known_at
        df["known_at_utc"] = pd.to_datetime(df["known_at_utc"], utc=True)
        df = df.sort_values(["event", "date"]).reset_index(drop=True)
        df = df[list(SCHEMA)]
    else:
        df = pd.DataFrame(columns=list(SCHEMA))

    report = validate_frame(df, list(EVENTS), start, end, known_value=False)
    source_count = len(FRED_RELEASES) + 2  # FOMC + options expiry
    report.add(
        CheckResult("fetch", not failures, "; ".join(failures) or f"{source_count} sources fetched")
    )
    return df, report
