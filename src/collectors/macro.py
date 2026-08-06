"""Macro and regime series from FRED. SPEC §2.2⑨ and §3.2.

Feeds the medium-term regime section, which is deterministic and involves no
LLM. Every series here is market-observed and daily, with decades of history, so
the regime layer works from the first backfill rather than accumulating forward
the way news does.

Three properties of FRED were verified against the live API on 2026-08-03 rather
than assumed, because each would corrupt the data quietly:

**Missing values arrive as the string ``"."``, not as null.** ``float(".")``
raises and ``pd.to_numeric(errors="coerce")`` would silently manufacture NaN.
:func:`_parse` drops them explicitly and the missing-ratio check then measures
something real.

**Treasury yields are absent on days NYSE trades.** The bond market closes for
Columbus Day and Veterans Day while the stock market does not — in 2024, DGS10
had 250 values against 252 XNYS trading days, with the two gaps on 2024-10-14
and 2024-11-11. Strict trading-day continuity would therefore fail on correct
data every year, so check three is a *coverage ratio* against US trading days.
See :func:`check_coverage`.

**Publication lags the observation date.** A value dated ``D`` is not knowable
during ``D``. ``known_at_utc`` is the next US session open after ``D`` ends —
the same rule CLAUDE.md applies to news, conservative by at most one session and
costless to a 120-day trend.

**The FX series follow the Federal Reserve, not NYSE.** ``DEXKOUS`` prints on
Good Friday, when the stock market is closed. Anything here that asks "was this
a trading day" therefore has to tolerate it; see :func:`check_coverage`.

FRED can revise history, which would make a stored value disagree with a later
fetch. All six series below are market-observed prints rather than estimated
aggregates, so revision is rare; and since CLAUDE.md forbids overwriting
``data/raw/``, a re-run lands beside the original rather than replacing it.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Mapping, Sequence

import pandas as pd
import requests

from src.collectors.validate import (
    CheckResult,
    ValidationReport,
    check_known_value,
    check_missing_ratio,
    check_schema,
    validate,
)
from src.util.session import next_tradeable_open, to_utc, trading_days

COLLECTOR = "macro"

_API = "https://api.stlouisfed.org/fred/series/observations"

# Series IDs confirmed against the FRED metadata endpoint on 2026-08-03, with
# their titles and start dates read back rather than recalled.
SERIES: Mapping[str, str] = {
    "us_10y": "DGS10",  # 10-Year Treasury constant maturity, %
    "yield_curve_10y2y": "T10Y2Y",  # 10Y minus 2Y, %. FRED computes it; we do not
    "dollar_index": "DTWEXBGS",  # Nominal Broad U.S. Dollar Index, Jan 2006 = 100
    "usdkrw": "DEXKOUS",  # South Korean won per 1 USD
    "wti": "DCOILWTICO",  # WTI Cushing, USD per barrel
    "vix": "VIXCLS",  # CBOE Volatility Index
}

# DXY proper is not a FRED series — it is ICE-proprietary. DTWEXBGS is the
# Federal Reserve's broad trade-weighted equivalent and is what SPEC §2.2① means
# by "the dollar". The two are correlated but not interchangeable, so the
# substitution is recorded here rather than left for someone to discover.

SCHEMA = {
    "date": "datetime64[s]",
    "series": "object",
    "series_id": "object",  # provenance travels with the row
    "value": "float64",
    "known_at_utc": "datetime64[ns, UTC]",
}

# After "." rows are dropped, a remaining null means the parse went wrong.
MISSING_THRESHOLDS = {"value": 0.0, "date": 0.0, "series": 0.0}

# 250/252 observed for DGS10 in 2024. 0.95 tolerates the bond-market holidays
# and a stray suspension without tolerating a half-broken fetch.
MIN_COVERAGE = 0.95

# How far behind the last session a series may be before it counts as stopped
# rather than lagging. WTI measured 4 trading days on 2026-08-05 — FRED
# redistributes an EIA series that publishes days behind — and the other five
# were current. 10 leaves room for a holiday week without letting a genuinely
# dead series pass unnoticed. The features these feed are 20- to 120-day
# trends, so a few stale days at the tail cost nothing; a series that quietly
# stopped six months ago would cost everything.
MAX_STALE_TRADING_DAYS = 10

# Cross-checked against home.treasury.gov, which publishes H.15 itself and which
# FRED redistributes. Verifying FRED against FRED would prove nothing.
KNOWN_VALUE = {
    "where": {"date": dt.date(2024, 1, 2), "series": "us_10y"},
    "column": "value",
    "expected": 3.95,
    "tolerance": 0.005,
}


class FredError(RuntimeError):
    """FRED answered, but not with observations."""


# --- validation ----------------------------------------------------------


def check_coverage(
    df: pd.DataFrame, series: Sequence[str], start: dt.date, end: dt.date
) -> CheckResult:
    """Check three, as a ratio rather than as exact trading-day equality.

    The strict version in :mod:`src.collectors.validate` asserts every trading
    day is present exactly once. That is right for KRX prices and wrong here:
    the bond market observes holidays NYSE does not, so a correct FRED series is
    missing a couple of NYSE days every year. Requiring exactness would fail
    real data annually, and a check that cries wolf is one that stops being read.

    A gap in the middle and a stale tail are judged separately, because they are
    different failures. An interior gap is missing data. A stale tail is a
    publication lag: WTI is an EIA series FRED redistributes days behind, and it
    is legitimately several sessions short of the present on every run. Measuring
    them together failed the check daily for data that was not missing anything.
    """
    expected = trading_days("US", start, end)
    if not expected:
        return CheckResult("coverage", False, f"no US trading days in {start}..{end}")

    problems: list[str] = []
    details: list[str] = []

    for name in series:
        subset = df[df["series"] == name] if "series" in df.columns else df.iloc[0:0]
        present = {d.date() for d in pd.to_datetime(subset["date"])} if len(subset) else set()

        if not present:
            problems.append(f"{name} has no rows at all")
            continue

        # A hole in the middle and a stale tail are different failures and are
        # judged separately. Measured 2026-08-05 over 2026-01-01..07-31: WTI had
        # 0 interior gaps and a 4-trading-day trailing lag, because FRED
        # redistributes an EIA series that publishes days behind. The other five
        # were complete to the last session.
        #
        # Counting the two together made the check fail on every run whose
        # window reached the present, for a series that was not missing anything
        # — and a check that fails daily is one that stops being read. Splitting
        # them makes it *more* sensitive to the failure that matters: a single
        # interior gap now shows up instead of being diluted by 140 good days.
        last = max(present)
        interior = [d for d in expected if d <= last and d not in present]
        trailing = [d for d in expected if d > last]

        covered = [d for d in expected if d <= last]
        ratio = (len(covered) - len(interior)) / len(covered) if covered else 0.0
        details.append(f"{name} {ratio:.1%}" + (f" (+{len(trailing)}d stale)" if trailing else ""))

        if ratio < MIN_COVERAGE:
            problems.append(
                f"{name} covers {ratio:.1%} of US trading days up to its last observation "
                f"({last}), need {MIN_COVERAGE:.0%} — {len(interior)} interior gap(s)"
            )

        if len(trailing) > MAX_STALE_TRADING_DAYS:
            problems.append(
                f"{name} last published {last}, {len(trailing)} trading days ago; "
                f"a lag beyond {MAX_STALE_TRADING_DAYS} means the series stopped rather "
                "than lagged"
            )

        # Stray dates are judged against weekends, not against the exchange
        # calendar. FRED's FX series follow the Federal Reserve, which prints on
        # days NYSE is closed — DEXKOUS has a rate on Good Friday 2024-03-29
        # while XNYS is shut. A row there is correct data. A row on a Saturday
        # is not, under any calendar, which is what makes weekends the right
        # test for "this series is indexed wrongly".
        weekend = sorted(d for d in present if d.weekday() >= 5)
        if weekend:
            problems.append(f"{name} has {len(weekend)} rows on weekends, e.g. {weekend[0]}")

        duplicated = subset["date"].duplicated().sum() if len(subset) else 0
        if duplicated:
            problems.append(f"{name} has {duplicated} duplicated dates")

    if problems:
        return CheckResult("coverage", False, "; ".join(problems))
    return CheckResult("coverage", True, ", ".join(details))


def validate_frame(
    df: pd.DataFrame,
    series: Sequence[str],
    start: dt.date,
    end: dt.date,
    *,
    known_value: bool = True,
) -> ValidationReport:
    """Run all four checks against a fetched or reloaded frame."""
    checks = [
        check_schema(df, SCHEMA),
        check_missing_ratio(df, MISSING_THRESHOLDS),
        check_coverage(df, series, start, end),
    ]
    if known_value:
        checks.append(check_known_value(df, **KNOWN_VALUE))
    return validate(COLLECTOR, checks)


# --- fetching ------------------------------------------------------------


def _parse(observations: list[dict], name: str, series_id: str) -> pd.DataFrame:
    """Turn FRED observations into the committed schema.

    ``"."`` marks a missing observation. Dropping those rows is deliberate: a
    coerced NaN would look like a collector fault, when in fact the market was
    simply closed that day.
    """
    rows = [
        {"date": o["date"], "series": name, "series_id": series_id, "value": float(o["value"])}
        for o in observations
        if o.get("value") not in (".", "", None)
    ]
    if not rows:
        return pd.DataFrame(columns=list(SCHEMA))

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[s]")
    # Midnight UTC after the observation date, resolved forward to the next US
    # session open. Deriving it from that date's own session close would crash
    # on the FX series, which print on days NYSE is shut (see _WEEKEND note).
    #
    # ``Timedelta(1, "D")`` rather than ``Timedelta(days=1)``: the keyword form
    # builds a NumPy timedelta with no unit, which NumPy has deprecated and says
    # it will make an error. Measured on pandas 2.3.3 / numpy 2.5.1 — the
    # keyword form warns, the positional form with an explicit unit does not.
    # Same value either way; this one is future-proof.
    df["known_at_utc"] = [
        next_tradeable_open("US", pd.Timestamp(d.date(), tz="UTC") + pd.Timedelta(1, "D"))
        for d in df["date"]
    ]
    return df[list(SCHEMA)]


def fetch(
    start: dt.date,
    end: dt.date,
    *,
    series: Mapping[str, str] | None = None,
    as_of: pd.Timestamp | None = None,
    api_key: str | None = None,
) -> tuple[pd.DataFrame, ValidationReport]:
    """Fetch macro series over ``[start, end]``.

    ``as_of`` is the look-ahead boundary: rows whose ``known_at_utc`` is at or
    after it are dropped. ``None`` means no boundary and suits a backfill, where
    the boundary is applied at feature-computation time instead.

    Returns the frame and its report rather than raising on a bad series, so one
    failing series does not cost the pipeline the other five.
    """
    series = dict(series or SERIES)
    key = api_key or os.environ.get("FRED_API_KEY")
    if not key:
        raise FredError("FRED_API_KEY is not set")

    frames: list[pd.DataFrame] = []
    failures: list[str] = []

    for name, series_id in series.items():
        response = requests.get(
            _API,
            params={
                "series_id": series_id,
                "api_key": key,
                "file_type": "json",
                "observation_start": start.isoformat(),
                "observation_end": end.isoformat(),
            },
            timeout=30,
        )
        if response.status_code != 200:
            failures.append(f"{name} ({series_id}): HTTP {response.status_code}")
            continue

        observations = response.json().get("observations")
        if observations is None:
            failures.append(f"{name} ({series_id}): no observations in response")
            continue

        parsed = _parse(observations, name, series_id)
        if not parsed.empty:
            frames.append(parsed)

    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=list(SCHEMA))

    if not df.empty:
        df = df.astype({k: v for k, v in SCHEMA.items() if k != "known_at_utc"})
        df["known_at_utc"] = pd.to_datetime(df["known_at_utc"], utc=True)
        if as_of is not None:
            df = df[df["known_at_utc"] < to_utc(as_of)].reset_index(drop=True)

    report = validate_frame(df, list(series), start, end, known_value=False)
    report.add(
        CheckResult("fetch", not failures, "; ".join(failures) or f"{len(series)} series fetched")
    )
    return df, report
