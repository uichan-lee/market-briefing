"""US daily OHLCV from Tiingo. SPEC §3.2.

Feeds SPEC §2.2① — the US → KR transmission section that opens the briefing —
and the `rel_strength` features in §5. Validation is defined before the fetching
logic, per CLAUDE.md.

The endpoint, the auth form and the response fields below were verified against
the live API on 2026-08-03 rather than written from memory; the documentation
page does not state how the token is passed.

Three things about this source shape the code.

**Both an adjusted and an unadjusted close are stored.** Tiingo returns them
separately, and each is wrong for the other's purpose. Returns must come from
``adj_close``, or a split reads as a crash. But ``adj_close`` is *restated*
every time a dividend is paid, so it cannot anchor the fourth check — today's
value for a past date is not the value that will be there next quarter. ``close``
never moves, which is why :data:`KNOWN_VALUE` pins that one.

**Tiingo fails loudly.** Unlike pykrx, a bad ticker or a bad token produces a
non-200 with a JSON message, so failure needs recording rather than inferring.
An unknown ticker returns 404 and is reported as a failed ticker, not a crash —
one delisted ETF must not cost the whole run.

**Dates come back as ISO instants at midnight UTC**
(``2024-01-02T00:00:00.000Z``) even though they denote a US trading *date*. They
are truncated to a date deliberately: treating that midnight as an instant would
place the bar 21 hours before the session it describes, and every look-ahead
guarantee downstream reads ``known_at_utc``, which is derived from the session
close instead.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Iterable, Sequence

import pandas as pd
import requests

from src.collectors.validate import (
    CheckResult,
    ValidationReport,
    check_known_value,
    check_missing_ratio,
    check_schema,
    check_trading_day_continuity,
    validate,
)
from src.util.session import session_close_utc, to_utc, trading_days

COLLECTOR = "us_price"

BASE_URL = "https://api.tiingo.com/tiingo/daily"

# SPEC §2.2①: US index and sector ETFs, with the KR sector each one is read
# across to. Kept here rather than in config because these are not a watchlist —
# they are the fixed left-hand side of the transmission mapping, and changing
# one changes what §2.2① means. Per-name US holdings go in the watchlist.
INDEX_ETFS = {
    "SPY": "S&P 500",
    "QQQ": "NASDAQ 100",
    "IWM": "Russell 2000 — read across to KOSDAQ small/mid",
    "SMH": "Semiconductors — read across to 삼성전자, SK하이닉스, 한미반도체",
    "XLK": "Technology",
    "XLE": "Energy — read across to 정유/조선",
    "XLF": "Financials",
    "XBI": "Biotech",
}

_RENAME = {"adjClose": "adj_close"}

SCHEMA = {
    "date": "datetime64[s]",
    "ticker": "object",
    "open": "float64",
    "high": "float64",
    "low": "float64",
    "close": "float64",
    "volume": "int64",
    # Split- and dividend-adjusted. Every return computed downstream uses this
    # column; `close` exists so the fourth check has something that stays put.
    "adj_close": "float64",
    # The earliest instant this row could legitimately be used. CLAUDE.md's
    # look-ahead rule is enforced against this column, not against `date`.
    "known_at_utc": "datetime64[ns, UTC]",
}

# A missing bar means the fetch was wrong, not that the market was quiet.
MISSING_THRESHOLDS = {
    "open": 0.0,
    "high": 0.0,
    "low": 0.0,
    "close": 0.0,
    "adj_close": 0.0,
    "volume": 0.0,
}

# Cross-checked against Yahoo Finance historical data on 2026-08-04 and matched
# exactly. That independence is the point: a value read back from Tiingo alone
# would catch a mismapped column or ticker but not Tiingo having been wrong
# about 2024-01-02 all along. kr_price pins its Samsung close against Naver
# Finance for the same reason.
#
# `close`, not `adj_close`, deliberately — see the module docstring. Tiingo
# restates adj_close on every dividend, so pinning it would turn each quarterly
# distribution into a spurious check failure.
KNOWN_VALUE = {
    "where": {"date": dt.date(2024, 1, 2), "ticker": "SPY"},
    "column": "close",
    "expected": 472.65,
}

_TIMEOUT = 30


class TiingoError(RuntimeError):
    """Tiingo answered with something other than usable price data."""


class TiingoRateLimit(TiingoError):
    """The request allowance is exhausted. Distinct from a bad ticker on purpose.

    The free tier allows 50 requests per hour, and the EOD endpoint takes one
    ticker per request (verified against the documentation 2026-08-04), so a
    watchlist near 50 symbols sits close to the ceiling. Once a 429 arrives,
    every remaining ticker in the run will also 429 — reporting them as N failed
    tickers would read as "the watchlist is broken" when the tickers are fine
    and the quota is not. The run stops at the first 429 and says so.
    """


# --- validation ----------------------------------------------------------


def validate_frame(
    df: pd.DataFrame,
    tickers: Sequence[str],
    start: dt.date,
    end: dt.date,
    *,
    known_value: bool = True,
) -> ValidationReport:
    """Run all four checks against a fetched or reloaded frame.

    Continuity is checked per ticker, since a frame holding several tickers has
    one row per ticker per trading day and the aggregate would look duplicated.
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
                f"no rows returned although {len(expected_days)} US trading days fall in "
                f"{start}..{end}",
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
        checks.append(check_known_value(df, **KNOWN_VALUE))

    return validate(COLLECTOR, checks)


# --- fetching ------------------------------------------------------------


def normalize(rows: Sequence[dict], ticker: str) -> pd.DataFrame:
    """Turn one Tiingo response into the committed schema."""
    if not rows:
        return pd.DataFrame(columns=list(SCHEMA))

    df = pd.DataFrame(list(rows)).rename(columns=_RENAME)

    missing = {"date", "open", "high", "low", "close", "volume", "adj_close"} - set(df.columns)
    if missing:
        # Reported rather than filled: a silently absent adj_close would make
        # every return in the report wrong by the size of the next dividend.
        raise TiingoError(f"{ticker}: response is missing {sorted(missing)}")

    df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_localize(None).astype("datetime64[s]")
    df["ticker"] = ticker
    df["known_at_utc"] = [session_close_utc("US", d.date()) for d in df["date"]]
    return df[list(SCHEMA)]


def _fetch_one(
    ticker: str, start: dt.date, end: dt.date, *, token: str, timeout: int
) -> list[dict]:
    response = requests.get(
        f"{BASE_URL}/{ticker}/prices",
        params={"startDate": start.isoformat(), "endDate": end.isoformat()},
        # Verified live 2026-08-03. The documentation says a token is needed but
        # not how to send it; this header form is the one that answers 200.
        headers={"Authorization": f"Token {token}"},
        timeout=timeout,
    )
    if response.status_code == 429:
        raise TiingoRateLimit(response.text[:200])
    if not response.ok:
        raise TiingoError(f"{ticker}: HTTP {response.status_code} {response.text[:200]}")
    return response.json()


def fetch(
    tickers: Iterable[str],
    start: dt.date,
    end: dt.date,
    *,
    as_of: pd.Timestamp | None = None,
    api_key: str | None = None,
    timeout: int = _TIMEOUT,
) -> tuple[pd.DataFrame, ValidationReport]:
    """Fetch daily OHLCV for ``tickers`` over ``[start, end]``.

    ``as_of`` is the look-ahead boundary and is not optional in spirit: any row
    whose ``known_at_utc`` is at or after it is dropped. Passing ``None`` means
    "no boundary" and is only appropriate for a historical backfill, where the
    boundary is applied later at feature-computation time.

    Returns the frame and its validation report rather than raising, so a failing
    collector can record the failure and let the pipeline publish a partial
    report. A missing key is the exception — that is a configuration error, not
    a data condition, and every ticker would fail identically.
    """
    token = api_key or os.environ.get("TIINGO_API_KEY")
    if not token:
        raise TiingoError("TIINGO_API_KEY is not set")

    tickers = list(tickers)
    frames: list[pd.DataFrame] = []
    failures: list[str] = []
    rate_limited: str | None = None

    for index, ticker in enumerate(tickers):
        try:
            rows = _fetch_one(ticker, start, end, token=token, timeout=timeout)
            frames.append(normalize(rows, ticker))
        except TiingoRateLimit as exc:
            # Stop rather than continue. Every remaining ticker would 429 too,
            # and a list of 40 "failed" tickers hides the one fact that matters.
            rate_limited = (
                f"quota exhausted after {index} of {len(tickers)} tickers; "
                f"{len(tickers) - index} left without data — {exc}"
            )
            break
        except (TiingoError, requests.RequestException) as exc:
            failures.append(f"{ticker} ({type(exc).__name__})")

    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=list(SCHEMA))

    if not df.empty:
        df = df.astype({k: v for k, v in SCHEMA.items() if k != "known_at_utc"})
        df["known_at_utc"] = pd.to_datetime(df["known_at_utc"], utc=True)
        if as_of is not None:
            df = df[df["known_at_utc"] < to_utc(as_of)].reset_index(drop=True)

    report = validate_frame(df, tickers, start, end, known_value=False)
    if rate_limited:
        report.add(CheckResult("rate_limit", False, rate_limited))
    if failures:
        report.add(
            CheckResult(
                "fetch",
                False,
                f"{len(failures)} of {len(tickers)} tickers failed: {', '.join(failures)}",
            )
        )
    else:
        report.add(CheckResult("fetch", True, f"{len(tickers)} tickers fetched"))

    return df, report
