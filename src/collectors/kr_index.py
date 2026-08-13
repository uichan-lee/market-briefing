"""KODEX 200 daily OHLCV, the PREREGISTRATION §8.5 benchmark. SPEC §3.1.

The 3-month gate asks whether the shadow portfolio beats **KODEX 200
buy-and-hold**, so the gate cannot be read without this series. Nothing else in
the pipeline consumes it: it is not a rating input, not a feature, and not a
watchlist member.

Validation is defined before the fetching logic, per CLAUDE.md.

**Why this is not part of `kr_price`.** Three reasons, each sufficient on its own.

1. **A different endpoint with different columns.** ETFs answer
   ``get_etf_ohlcv_by_date``, not ``get_market_ohlcv_by_date``. Verified live
   2026-08-13: it returns ``NAV 시가 고가 저가 종가 거래량 거래대금 기초지수`` —
   no ``등락률``, which `kr_price`'s schema requires, and two columns a stock
   does not have. Feeding an ETF through the stock path would rename by a map
   that does not fit and fail in the schema check, or worse, not fail.
2. **The ticker must not reach the watchlist.** Every KR fetcher takes its
   symbols from ``load_watchlist``, and everything downstream — ``compute()``,
   ``rate()``, the ⑥ rating table — reads that same list. Adding ``069500``
   there to get its prices would put an index ETF in the briefing as though it
   were a stock to hold an opinion about.
3. **The benchmark is evaluation input, not pipeline input.** Keeping it in its
   own module and its own ``data/raw/kr/benchmark/`` directory makes that
   visible in the layout rather than only in prose.

**What `기초지수` buys, and why the known-value check uses it.** The column is
the KOSPI 200 level the ETF tracks, and the *index* endpoint publishes the same
number independently of the ETF endpoint. So one hardcoded value ties two KRX
endpoints together — the same shape of check as `kr_flow`'s implied close, which
ties ``market_cap ÷ shares_outstanding`` to a price `kr_price` pins separately.
A value read back from the same endpoint it validates would only catch pykrx
changing its mind, never pykrx being wrong today.

**Prices are split-adjusted and *not* distribution-adjusted.** Same as
`kr_price`; there is no ``adj_close`` on the KR side. KODEX 200 pays
distributions, so this series understates the benchmark's total return —
PREREGISTRATION §8.5 records that asymmetry, and that it favours the shadow
portfolio, so a narrow win reads as no win.
"""

from __future__ import annotations

import datetime as dt

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
from src.util.krx import KrxSessionError, import_pykrx_stock
from src.util.session import session_close_utc, to_utc, trading_days

COLLECTOR = "kr_index"

# PREREGISTRATION §8.5's named benchmark, and the KOSPI 200 index it tracks.
# 1028 is KRX's own code for 코스피 200 on the index endpoint.
BENCHMARK_TICKER = "069500"
_UNDERLYING_INDEX = "1028"

# Verified live against the endpoint on 2026-08-13, not inferred from kr_price.
# `NAV`, `거래대금` and `기초지수` are dropped by the keep-list at the end of
# `_normalize`: NAV and 거래대금 are not needed to compute a return, and 기초지수
# is kept only long enough for the known-value check to read it.
_RENAME = {
    "시가": "open",
    "고가": "high",
    "저가": "low",
    "종가": "close",
    "거래량": "volume",
    "기초지수": "index_level",
}

SCHEMA = {
    "date": "datetime64[s]",
    "ticker": "object",
    # uint32/uint64 off the wire; cast to int64 to match kr_price, so the two
    # frames can be concatenated and compared without a dtype surprise.
    "open": "int64",
    "high": "int64",
    "low": "int64",
    "close": "int64",
    "volume": "int64",
    # The KOSPI 200 level this ETF tracks. Stored rather than discarded because
    # it is what the known-value check cross-references against a second
    # endpoint — see the module docstring.
    "index_level": "float64",
    # The earliest instant this row could legitimately be used. CLAUDE.md's
    # look-ahead rule is enforced against this column, not against `date`.
    "known_at_utc": "datetime64[ns, UTC]",
}

# An index ETF trades every session the market is open, so every column must be
# complete. There is no 등락률 here to tolerate, unlike kr_price.
MISSING_THRESHOLDS = {
    "open": 0.0,
    "high": 0.0,
    "low": 0.0,
    "close": 0.0,
    "volume": 0.0,
    "index_level": 0.0,
}

# The fourth CLAUDE.md check. `index_level` rather than `close`, because the
# same figure is published by a different KRX endpoint: on 2024-01-02
# `get_index_ohlcv_by_date(..., "1028")` closes at 360.55 and this endpoint's
# 기초지수 reads 360.55. Confirmed live 2026-08-13 — one value, two endpoints.
#
# UNVERIFIED: this pair agrees inside KRX but has not been checked against a
# source outside KRX. `kr_price`'s equivalent was cross-checked against Naver
# Finance; the same could not be done here because that host is unreachable
# from this environment. What the check currently proves is that the ETF
# endpoint's tracking column and the index endpoint agree, which would still
# catch a mis-mapped rename — the failure mode it mainly exists for.
KNOWN_VALUE = {
    "where": {"date": dt.date(2024, 1, 2), "ticker": BENCHMARK_TICKER},
    "column": "index_level",
    "expected": 360.55,
}

_SLEEP_SECONDS = 1.0


# --- validation ----------------------------------------------------------


def validate_frame(
    df: pd.DataFrame,
    start: dt.date,
    end: dt.date,
    *,
    known_value: bool = True,
) -> ValidationReport:
    """Run all four checks against a fetched or reloaded frame.

    One ticker, so continuity is checked on the whole frame rather than per
    ticker as `kr_price` must.
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
        checks.append(CheckResult("not_empty", True, f"{len(df)} rows for {BENCHMARK_TICKER}"))

    checks.append(check_trading_day_continuity(df, "KR", "date", start, end))

    if known_value:
        checks.append(check_known_value(df, **KNOWN_VALUE))

    return validate(COLLECTOR, checks)


# --- fetching ------------------------------------------------------------


def _normalize(raw: pd.DataFrame) -> pd.DataFrame:
    """Turn one pykrx ETF frame into the committed schema."""
    if raw.empty:
        return pd.DataFrame(columns=list(SCHEMA))

    df = raw.rename(columns=_RENAME).reset_index(names="date")
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[s]")
    df["ticker"] = BENCHMARK_TICKER
    df["known_at_utc"] = [session_close_utc("KR", d.date()) for d in df["date"]]
    return df[list(SCHEMA)]


def fetch(
    start: dt.date,
    end: dt.date,
    *,
    as_of: pd.Timestamp | None = None,
    sleep_seconds: float = _SLEEP_SECONDS,
) -> tuple[pd.DataFrame, ValidationReport]:
    """Fetch KODEX 200 daily OHLCV over ``[start, end]``.

    ``as_of`` is the look-ahead boundary and is not optional in spirit: any row
    whose ``known_at_utc`` is at or after it is dropped. Passing ``None`` means
    "no boundary" and suits only a historical backfill.

    Returns the frame and its report rather than raising, so a failure is
    recorded and the pipeline still publishes.
    """
    del sleep_seconds  # one ticker, one request — nothing to pace against

    try:
        stock = import_pykrx_stock()
    except KrxSessionError as exc:
        # pykrx logs in at import, so a refused login would abort the pipeline
        # instead of leaving a recorded failure behind. See src/util/krx.py.
        report = validate_frame(pd.DataFrame(columns=list(SCHEMA)), start, end, known_value=False)
        report.add(CheckResult("krx_session", False, str(exc)))
        return pd.DataFrame(columns=list(SCHEMA)), report

    raw = stock.get_etf_ohlcv_by_date(
        fromdate=start.strftime("%Y%m%d"),
        todate=end.strftime("%Y%m%d"),
        ticker=BENCHMARK_TICKER,
    )
    df = _normalize(raw)

    if not df.empty:
        df = df.astype({k: v for k, v in SCHEMA.items() if k != "known_at_utc"})
        df["known_at_utc"] = pd.to_datetime(df["known_at_utc"], utc=True)
        if as_of is not None:
            df = df[df["known_at_utc"] < to_utc(as_of)].reset_index(drop=True)

    report = validate_frame(df, start, end, known_value=False)
    if raw.empty:
        report.add(
            CheckResult(
                "fetch",
                False,
                f"{BENCHMARK_TICKER} returned nothing for {start}..{end}",
            )
        )
    else:
        report.add(CheckResult("fetch", True, f"{len(df)} sessions fetched"))

    return df, report
