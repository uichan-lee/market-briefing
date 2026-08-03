"""KR daily OHLCV from pykrx. SPEC §3.1.

Validation is defined before the fetching logic, per CLAUDE.md. The four checks
in :mod:`src.collectors.validate` are applied by :func:`validate_frame`, which a
caller may run against any frame — including one loaded back from ``data/raw/``.

Three things about this source are worth knowing before reading the code.

**pykrx fails by returning an empty frame.** A failed request prints an error and
yields ``DataFrame()`` rather than raising. Observed directly: as of 2026-08-03
every ``data.krx.co.kr`` endpoint answers ``HTTP 400 LOGOUT`` without a session,
and the wrapper turns that into an empty result. Silent failure is the worst
outcome in this project, so :func:`validate_frame` treats "empty while the
calendar says there were trading days" as a failure rather than as no data.

**Prices are split-adjusted.** ``adjusted=True`` is pykrx's default and is kept,
because unadjusted prices produce wrong returns across a split. The cost is that
a past date's value can be restated later. That stays visible rather than silent:
CLAUDE.md forbids overwriting ``data/raw/``, so a re-run lands beside the
original and the two can be compared.

**Column names arrive in Korean** and are mapped explicitly by :data:`_RENAME`.
A positional or implicit rename here would be exactly the kind of well-formed
but wrong data the fourth check exists to catch.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Iterable, Sequence

import pandas as pd

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

COLLECTOR = "kr_price"

_RENAME = {
    "시가": "open",
    "고가": "high",
    "저가": "low",
    "종가": "close",
    "거래량": "volume",
    "등락률": "change_pct",
}

SCHEMA = {
    "date": "datetime64[s]",
    "ticker": "object",
    "open": "int64",
    "high": "int64",
    "low": "int64",
    "close": "int64",
    "volume": "int64",
    "change_pct": "float64",
    # The earliest instant this row could legitimately be used. CLAUDE.md's
    # look-ahead rule is enforced against this column, not against `date`.
    "known_at_utc": "datetime64[ns, UTC]",
}

# Price and volume must be complete; a missing bar means the fetch was wrong,
# not that the market was quiet. 등락률 is derived by KRX and tolerated as absent
# on the first row of a range, where there is no prior close to compare against.
MISSING_THRESHOLDS = {
    "open": 0.0,
    "high": 0.0,
    "low": 0.0,
    "close": 0.0,
    "volume": 0.0,
    "change_pct": 0.05,
}

# Cross-checked against Naver Finance on 2026-08-03, which sources independently
# of KRX. A value taken from pykrx itself would only detect pykrx changing its
# mind, not pykrx being wrong today.
KNOWN_VALUE = {
    "where": {"date": dt.date(2024, 1, 2), "ticker": "005930"},
    "column": "close",
    "expected": 79_600.0,
}

# pykrx scrapes KRX; SPEC §3.1 notes a sleep between calls is required.
_SLEEP_SECONDS = 1.0


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

    expected_days = trading_days("KR", start, end)
    if expected_days and df.empty:
        checks.append(
            CheckResult(
                "not_empty",
                False,
                f"no rows returned although {len(expected_days)} KR trading days fall in "
                f"{start}..{end}; pykrx returns an empty frame on a failed request",
            )
        )
    else:
        checks.append(
            CheckResult("not_empty", True, f"{len(df)} rows for {len(tickers)} ticker(s)")
        )

    for ticker in tickers:
        subset = df[df["ticker"] == ticker] if "ticker" in df.columns else df.iloc[0:0]
        result = check_trading_day_continuity(subset, "KR", "date", start, end)
        checks.append(CheckResult(f"{result.name}[{ticker}]", result.passed, result.detail))

    if known_value:
        checks.append(check_known_value(df, **KNOWN_VALUE))

    return validate(COLLECTOR, checks)


# --- fetching ------------------------------------------------------------


def _normalize(raw: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Turn one pykrx frame into the committed schema."""
    if raw.empty:
        return pd.DataFrame(columns=list(SCHEMA))

    df = raw.rename(columns=_RENAME).reset_index(names="date")
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[s]")
    df["ticker"] = ticker
    df["known_at_utc"] = [session_close_utc("KR", d.date()) for d in df["date"]]
    return df[list(SCHEMA)]


def fetch(
    tickers: Iterable[str],
    start: dt.date,
    end: dt.date,
    *,
    as_of: pd.Timestamp | None = None,
    sleep_seconds: float = _SLEEP_SECONDS,
) -> tuple[pd.DataFrame, ValidationReport]:
    """Fetch daily OHLCV for ``tickers`` over ``[start, end]``.

    ``as_of`` is the look-ahead boundary and is not optional in spirit: any row
    whose ``known_at_utc`` is at or after it is dropped. Passing ``None`` means
    "no boundary" and is only appropriate for a historical backfill, where the
    boundary is applied later at feature-computation time.

    Returns the frame and its validation report rather than raising, so a
    failing collector can record the failure and let the pipeline publish a
    partial report.
    """
    from pykrx import stock  # imported here so the module is importable offline

    tickers = list(tickers)
    frames: list[pd.DataFrame] = []
    failures: list[str] = []

    for index, ticker in enumerate(tickers):
        if index:
            time.sleep(sleep_seconds)
        raw = stock.get_market_ohlcv_by_date(
            fromdate=start.strftime("%Y%m%d"),
            todate=end.strftime("%Y%m%d"),
            ticker=ticker,
            adjusted=True,
        )
        if raw.empty:
            failures.append(ticker)
            continue
        frames.append(_normalize(raw, ticker))

    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=list(SCHEMA))

    if not df.empty:
        df = df.astype({k: v for k, v in SCHEMA.items() if k != "known_at_utc"})
        df["known_at_utc"] = pd.to_datetime(df["known_at_utc"], utc=True)
        if as_of is not None:
            df = df[df["known_at_utc"] < to_utc(as_of)].reset_index(drop=True)

    report = validate_frame(df, tickers, start, end, known_value=False)
    if failures:
        report.add(
            CheckResult(
                "fetch",
                False,
                f"{len(failures)} of {len(tickers)} tickers returned nothing: "
                f"{', '.join(failures)}",
            )
        )
    else:
        report.add(CheckResult("fetch", True, f"{len(tickers)} tickers fetched"))

    return df, report
