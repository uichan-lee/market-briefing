"""US daily OHLCV from Alpaca. SPEC §3.2, the replacement for the Tiingo path.

Why this exists alongside ``us_price.py``
-----------------------------------------
Tiingo's EOD endpoint takes **one ticker per request** and its free tier allows
**50 requests per hour**. The watchlist is 40 US symbols plus the 8 index ETFs
in :data:`us_price.INDEX_ETFS`, so a single run costs 48 of those 50. There is
no headroom for a retry, and a 429 in the middle of a run leaves the rest of the
watchlist without data.

Alpaca's bars endpoint takes a **comma-separated list of symbols** and allows
**200 requests per minute**, so the same 48 symbols cost one request per page
instead of 48. That is the entire reason for the switch: it is a shape change,
not a quality upgrade.

The unresolved question this module cannot answer by itself
-----------------------------------------------------------
Alpaca's own documentation says two things that do not agree:

* the plan comparison lists the free Basic plan as **IEX only** for equities;
* the Market Data FAQ says that for *historical* queries, ``end`` merely has to
  be at least 15 minutes old to query **SIP** without a subscription.

The difference is not cosmetic. IEX is one exchange; SIP is the consolidated
tape of all of them. Alpaca's own FAQ gives the example of AAPL on 2023-09-29:
**923,134 shares on IEX against 51,861,083 on SIP**, a factor of 56. IEX-only
daily bars would give this project the wrong volume for every US name and a
close that is a single venue's last print rather than the official close.

**This is decided empirically, not by reading harder.** :func:`probe_feed` runs
the test and is the first thing to run once a key exists; MANUAL-TASKS.md §1
carries the procedure. The check that settles it already exists — SPY's
2024-01-02 close of 472.65, independently confirmed against Yahoo Finance — so
a feed serving IEX bars fails the fourth check rather than passing quietly.

Until that probe has been run against a real key, every ``# UNVERIFIED`` marker
below stands and ``us_price.py`` remains the collector in use.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Iterable, Sequence

import pandas as pd
import requests

from src.collectors.us_price import KNOWN_VALUE, MISSING_THRESHOLDS, SCHEMA
from src.collectors.validate import (
    CheckResult,
    ValidationReport,
    check_missing_ratio,
    check_schema,
    check_trading_day_continuity,
    validate,
)
from src.util.session import session_close_utc, to_utc, trading_days

COLLECTOR = "us_price_alpaca"

# Documented at https://docs.alpaca.markets/us/reference/stockbars, read
# 2026-08-04. Host, path, parameter names, header names and response shape all
# come from that page rather than from memory.
BASE_URL = "https://data.alpaca.markets/v2/stocks/bars"

# Alpaca names its bar fields with single letters.
_RENAME = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}

# The endpoint caps a page at 10,000 bars and paginates with next_page_token.
# 48 symbols across a year is ~12,000 bars, so pagination is required rather
# than optional — a backfill that ignored it would silently stop mid-year.
_PAGE_LIMIT = 10_000

_TIMEOUT = 30

# A day's worth of pages, doubled for the raw and adjusted passes, against a
# 200/minute allowance. The guard exists to turn a pagination bug into a loud
# failure instead of an unbounded loop against a metered API.
_MAX_PAGES = 50


class AlpacaError(RuntimeError):
    """Alpaca answered with something other than usable price data."""


class AlpacaFeedError(AlpacaError):
    """The requested feed was refused — almost always SIP without entitlement."""


def _headers() -> dict[str, str]:
    key = os.environ.get("ALPACA_API_KEY_ID")
    secret = os.environ.get("ALPACA_API_SECRET_KEY")
    if not (key and secret):
        raise AlpacaError(
            "ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY are not set. "
            "Configuration error, not a data condition — every symbol would fail alike."
        )
    # UNVERIFIED: header names are as documented; not yet exercised against a
    # live key. A wrong header name returns 401/403, which _request surfaces.
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def _request(params: dict, *, timeout: int) -> dict:
    response = requests.get(BASE_URL, params=params, headers=_headers(), timeout=timeout)
    if response.status_code in (401, 403):
        # 403 on a sip request is the entitlement answer, and it is the one
        # outcome worth naming separately: it means the free plan does not serve
        # consolidated history and the switch cannot go ahead on this plan.
        raise AlpacaFeedError(
            f"HTTP {response.status_code} for feed={params.get('feed')!r}: {response.text[:200]}"
        )
    if not response.ok:
        raise AlpacaError(f"HTTP {response.status_code}: {response.text[:200]}")
    return response.json()


def _fetch_bars(
    symbols: Sequence[str],
    start: dt.date,
    end: dt.date,
    *,
    adjustment: str,
    feed: str,
    timeout: int,
) -> dict[str, list[dict]]:
    """All daily bars for ``symbols`` over ``[start, end]``, following pagination.

    Returns a symbol → bars mapping. One request covers every symbol, which is
    the whole point of this module.
    """
    collected: dict[str, list[dict]] = {}
    params = {
        "symbols": ",".join(symbols),
        "timeframe": "1Day",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "adjustment": adjustment,
        "feed": feed,
        "limit": _PAGE_LIMIT,
        "sort": "asc",
    }

    for _page in range(_MAX_PAGES):
        payload = _request(params, timeout=timeout)
        for symbol, bars in (payload.get("bars") or {}).items():
            collected.setdefault(symbol, []).extend(bars)

        token = payload.get("next_page_token")
        if not token:
            return collected
        params = {**params, "page_token": token}

    raise AlpacaError(
        f"pagination did not terminate after {_MAX_PAGES} pages; refusing to keep requesting"
    )


def normalize(raw: dict[str, list[dict]], adjusted: dict[str, list[dict]]) -> pd.DataFrame:
    """Merge the unadjusted and adjusted passes into the committed schema.

    Two passes are needed because Alpaca adjusts *the whole bar* rather than
    adding a column the way Tiingo does. ``adjustment=raw`` supplies the close
    that :data:`us_price.KNOWN_VALUE` pins — a value that never moves — and
    ``adjustment=all`` supplies the ``adj_close`` every downstream return uses.
    Storing only one of them would break either the fourth check or every
    return, which is the same trade-off recorded in ``us_price.py``.
    """
    frames: list[pd.DataFrame] = []

    for symbol, bars in sorted(raw.items()):
        if not bars:
            continue
        df = pd.DataFrame(bars).rename(columns=_RENAME)

        missing = {"t", "open", "high", "low", "close", "volume"} - set(df.columns)
        if missing:
            raise AlpacaError(f"{symbol}: bars are missing {sorted(missing)}")

        # UNVERIFIED: for 1Day bars `t` is documented as RFC-3339 but the page
        # does not say which instant of the session it denotes. Converting to
        # America/New_York and taking the date is correct whether Alpaca stamps
        # ET midnight or the session open, and wrong only if it stamps the
        # *close* in UTC — which would shift every bar back a day and is exactly
        # what the trading-day continuity check would catch.
        df["date"] = (
            pd.to_datetime(df["t"], utc=True)
            .dt.tz_convert("America/New_York")
            .dt.normalize()
            .dt.tz_localize(None)
            .astype("datetime64[s]")
        )
        df["ticker"] = symbol

        adj = {b["t"]: b["c"] for b in adjusted.get(symbol, [])}
        df["adj_close"] = [adj.get(t) for t in df["t"]]
        if df["adj_close"].isna().any():
            n = int(df["adj_close"].isna().sum())
            raise AlpacaError(
                f"{symbol}: {n} bar(s) present in the raw pass have no adjusted counterpart; "
                "a silently absent adj_close makes every return wrong by the next dividend"
            )

        df["known_at_utc"] = [session_close_utc("US", d.date()) for d in df["date"]]
        frames.append(df[list(SCHEMA)])

    if not frames:
        return pd.DataFrame(columns=list(SCHEMA))
    return pd.concat(frames, ignore_index=True)


def validate_frame(
    df: pd.DataFrame,
    tickers: Sequence[str],
    start: dt.date,
    end: dt.date,
    *,
    known_value: bool = True,
) -> ValidationReport:
    """The same four checks as the Tiingo path, against the same schema.

    Deliberately reuses :data:`us_price.SCHEMA` and :data:`us_price.KNOWN_VALUE`.
    A second source is only a drop-in replacement if it is held to the identical
    contract, and the known value is what proves the two agree.
    """
    checks: list[CheckResult] = [
        check_schema(df, SCHEMA),
        check_missing_ratio(df, MISSING_THRESHOLDS),
    ]

    expected_days = trading_days("US", start, end)
    if expected_days and df.empty:
        checks.append(
            CheckResult(
                "not_empty",
                False,
                f"no rows although {len(expected_days)} US trading days fall in {start}..{end}",
            )
        )
    else:
        checks.append(
            CheckResult("not_empty", True, f"{len(df)} rows for {len(tickers)} ticker(s)")
        )

    for ticker in tickers:
        subset = df[df["ticker"] == ticker] if "ticker" in df.columns else df.iloc[0:0]
        result = check_trading_day_continuity(subset, "US", "date", start, end)
        checks.append(CheckResult(f"{result.name}[{ticker}]", result.passed, result.detail))

    if known_value:
        from src.collectors.validate import check_known_value

        checks.append(check_known_value(df, **KNOWN_VALUE))

    return validate(COLLECTOR, checks)


def fetch(
    tickers: Iterable[str],
    start: dt.date,
    end: dt.date,
    *,
    as_of: pd.Timestamp | None = None,
    feed: str = "sip",
    timeout: int = _TIMEOUT,
) -> tuple[pd.DataFrame, ValidationReport]:
    """Fetch daily OHLCV for ``tickers`` over ``[start, end]``.

    ``feed`` defaults to ``"sip"`` — the consolidated tape — because IEX-only
    bars carry a fraction of true volume and are not usable here. If the plan
    does not entitle SIP, this raises :class:`AlpacaFeedError` rather than
    quietly falling back to ``iex``: a silent downgrade would put believable but
    wrong volume into every report.

    ``as_of`` is the look-ahead boundary; rows whose ``known_at_utc`` is at or
    after it are dropped. ``None`` means no boundary and suits only a historical
    backfill where the boundary is applied at feature-computation time.
    """
    tickers = list(tickers)
    if not tickers:
        return pd.DataFrame(columns=list(SCHEMA)), validate(COLLECTOR, [])

    raw = _fetch_bars(tickers, start, end, adjustment="raw", feed=feed, timeout=timeout)
    adjusted = _fetch_bars(tickers, start, end, adjustment="all", feed=feed, timeout=timeout)

    df = normalize(raw, adjusted)

    if not df.empty:
        df = df.astype({k: v for k, v in SCHEMA.items() if k != "known_at_utc"})
        df["known_at_utc"] = pd.to_datetime(df["known_at_utc"], utc=True)
        if as_of is not None:
            df = df[df["known_at_utc"] < to_utc(as_of)].reset_index(drop=True)

    report = validate_frame(df, tickers, start, end, known_value=False)

    returned = set(df["ticker"]) if not df.empty else set()
    absent = sorted(set(tickers) - returned)
    if absent:
        detail = f"{len(absent)} of {len(tickers)} returned no bars: {absent}"
        report.add(CheckResult("fetch", False, detail))
    else:
        report.add(CheckResult("fetch", True, f"{len(tickers)} tickers in 2 requests"))

    return df, report


def probe_feed(timeout: int = _TIMEOUT) -> str:
    """Answer the one question the documentation does not: SIP or IEX?

    Run this first, before trusting anything else in this module. It fetches
    SPY for 2024-01-02 — the date :data:`us_price.KNOWN_VALUE` pins against an
    independently-confirmed close of 472.65 — and reports what the plan actually
    serves. Returns a human-readable verdict; raises nothing on a refused feed,
    because "SIP is refused" is a finding rather than an error here.
    """
    day = dt.date(2024, 1, 2)
    lines: list[str] = []

    for feed in ("sip", "iex"):
        try:
            bars = _fetch_bars(["SPY"], day, day, adjustment="raw", feed=feed, timeout=timeout)
        except AlpacaFeedError as exc:
            lines.append(f"{feed:4}: REFUSED — {exc}")
            continue
        except AlpacaError as exc:
            lines.append(f"{feed:4}: ERROR — {exc}")
            continue

        rows = bars.get("SPY") or []
        if not rows:
            lines.append(f"{feed:4}: no bars returned for {day}")
            continue

        bar = rows[0]
        close, volume = bar.get("c"), bar.get("v")
        expected = KNOWN_VALUE["expected"]
        verdict = "MATCHES the confirmed close" if close == expected else "DOES NOT MATCH"
        lines.append(f"{feed:4}: close={close} (expected {expected}, {verdict}), volume={volume:,}")

    lines.append("")
    lines.append(
        "Consolidated SPY volume on a normal session runs in the tens of millions. "
        "A volume near a million means IEX-only bars, whatever the feed parameter said, "
        "and this collector must not be used on that plan."
    )
    return "\n".join(lines)
