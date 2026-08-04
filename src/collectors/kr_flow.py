"""KR investor flows, short interest, market cap and fundamentals. SPEC §3.1.

This is the collector the KRX login was blocking. It supplies **55% of the
§2.2⑥ rating weight** — `foreign_flow_5d` (0.30), `inst_flow_5d` (0.15),
`short_ratio` (−0.10) and `valuation_band` (0.05) — and without it the surviving
45% sits under `min_weight_coverage`, forcing every ticker to 관망.

Four endpoints, one collector. They share a grain (ticker × trading day), they
are fetched in the same pass, they are gated by the same credential, and they
feed the same block of the rating. Splitting them would quadruple the sleep
budget against a scraped source for no gain.

SPEC §3.1 calls daily net buying by investor type this project's structural
edge: it has no US equivalent, and it is a signal obtained without an LLM.

What is worth knowing before reading the code
---------------------------------------------
**pykrx fails by returning an empty frame**, exactly as in ``kr_price``. A
failed request prints and yields ``DataFrame()`` rather than raising, so "empty
while the calendar says there were sessions" is treated as failure rather than
as an absence of news.

**Net purchases sum to zero, by construction.** Every share bought is a share
sold, so 기관합계 + 기타법인 + 개인 + 외국인합계 is zero on every row —
``전체`` is literally always ``0`` in the response. :func:`check_flow_identity`
asserts it. This is the cheapest possible detector for a mismapped column: swap
two investor types and the totals still look plausible, but the identity breaks
immediately.

**Market cap divided by shares outstanding is the close.** That ties this
collector to ``kr_price``, whose close for 005930 on 2024-01-02 was
cross-checked against Naver Finance. :func:`check_implied_close` re-derives it
here, so the two collectors cannot silently disagree about the same day.

**Values are in KRW and are large.** 삼성전자's market cap is ~4.75e14, which
exceeds float32 and loses precision in float64 arithmetic beyond ~9e15. The
schema keeps them as int64 rather than letting a float creep in.
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
from src.util.krx import KrxSessionError, import_pykrx_stock
from src.util.session import session_close_utc, to_utc, trading_days

COLLECTOR = "kr_flow"

# Korean column names are mapped explicitly. A positional rename here would be
# precisely the well-formed-but-wrong data the fourth check exists to catch.
_RENAME_FLOW = {
    "외국인합계": "foreign_net",
    "기관합계": "inst_net",
    "개인": "retail_net",
    "기타법인": "other_corp_net",
}
_RENAME_CAP = {
    "시가총액": "market_cap",
    "거래량": "volume",
    "거래대금": "trading_value",
    "상장주식수": "shares_outstanding",
}
_RENAME_SHORT = {
    "공매도잔고": "short_balance",
    "공매도금액": "short_value",
    "비중": "short_ratio_pct",
}
_RENAME_FUND = {
    "BPS": "bps",
    "PER": "per",
    "PBR": "pbr",
    "EPS": "eps",
    "DIV": "div_yield",
    "DPS": "dps",
}

SCHEMA = {
    "date": "datetime64[s]",
    "ticker": "object",
    # Net purchases in KRW. SPEC §5: foreign_flow_5d and inst_flow_5d are these
    # accumulated over 5 days and divided by 5-day cumulative trading_value.
    #
    # Nullable, though MISSING_THRESHOLDS still requires them complete. A halted
    # session gets a genuine zero (see normalize), but a session that traded and
    # has no flow row is a real gap — and a non-nullable dtype would turn that
    # into an IntCastingNaNError from inside the cast, which is a raise rather
    # than the recorded failure CLAUDE.md asks for. The nullable type lets the
    # gap survive long enough for missing_ratio to report it.
    "foreign_net": "Int64",
    "inst_net": "Int64",
    "retail_net": "Int64",
    "other_corp_net": "Int64",
    # The denominator for both flow features, and the size controls.
    "trading_value": "int64",
    "volume": "int64",
    "market_cap": "int64",
    "shares_outstanding": "int64",
    # SPEC §5: short_ratio is balance ÷ shares outstanding. KRX also publishes
    # its own 비중, kept alongside so the derived value can be checked against it.
    # Nullable: short-interest balance is not published for every name, and a
    # numpy int64 cannot hold the absence. Declaring the nullable type here is
    # what keeps check_schema honest rather than making it tolerate surprises.
    "short_balance": "Int64",
    "short_value": "Int64",
    "short_ratio_pct": "float64",
    # Fundamentals. valuation_band (SPEC §5) is a 3-year percentile of pbr.
    # Nullable for the same reason: KRX publishes no fundamentals against
    # negative earnings, and that is a real state rather than a fetch failure.
    "bps": "Int64",
    "per": "float64",
    "pbr": "float64",
    "eps": "Int64",
    "div_yield": "float64",
    "dps": "Int64",
    # The earliest instant this row could legitimately be used. CLAUDE.md's
    # look-ahead rule is enforced against this column, not against `date`.
    "known_at_utc": "datetime64[ns, UTC]",
}

# Flows, cap and volume must be complete on a session — a hole means the fetch
# was wrong, not that nobody traded. Fundamentals are tolerated sparse: KRX
# publishes no PER for a company with negative earnings, and that is a real
# state of the world rather than a gap. Short interest is tolerated likewise;
# balance reporting lags and does not exist for every name.
MISSING_THRESHOLDS = {
    "foreign_net": 0.0,
    "inst_net": 0.0,
    "retail_net": 0.0,
    "trading_value": 0.0,
    "market_cap": 0.0,
    "shares_outstanding": 0.0,
    "short_balance": 0.20,
    "short_ratio_pct": 0.20,
    "per": 0.50,
    "pbr": 0.20,
}

# Samsung Electronics' common shares outstanding on 2024-01-02. Published by
# DART, Samsung IR and Naver Finance alike, and it moves only on an announced
# issuance or buyback cancellation — so unlike a price it can be pinned.
#
# It is also the strongest anchor available here: market_cap divided by this
# figure is exactly 79,600, which is the close kr_price pins against Naver
# Finance. One value therefore ties two collectors and an outside source
# together. check_implied_close asserts that relationship separately.
KNOWN_VALUE = {
    "where": {"date": dt.date(2024, 1, 2), "ticker": "005930"},
    "column": "shares_outstanding",
    "expected": 5_969_782_550,
}

_IMPLIED_CLOSE = {
    "where": {"date": dt.date(2024, 1, 2), "ticker": "005930"},
    "expected": 79_600,
}

# pykrx scrapes KRX; SPEC §3.1 requires a sleep between calls. This sleeps
# before *every* request, not once per ticker, and that distinction was measured
# rather than assumed: sleeping only between tickers still issues the four
# endpoint calls back-to-back, and on a 31-ticker run KRX began returning an
# HTML error page instead of JSON from roughly the 27th ticker onward. pykrx
# surfaces that as `Expecting value: line 13 column 1` and an empty frame, so
# the symptom is four tickers silently missing rather than an error.
#
# 4 requests per ticker means this dominates runtime: 31 tickers is about two
# minutes. That is the cost of not being rate-limited, and it is paid once a day.
_SLEEP_SECONDS = 1.0

# One retry, because the block above is transient and losing a ticker for the
# day is worse than waiting. Bounded rather than open-ended: a source that is
# genuinely down should be reported, not hammered.
_RETRIES = 1
_RETRY_BACKOFF = 3.0


# --- validation ----------------------------------------------------------


def check_flow_identity(df: pd.DataFrame) -> CheckResult:
    """Net purchases across all investor types must sum to zero on every row.

    An accounting identity, not a heuristic: every share bought is a share sold.
    KRX's own ``전체`` column is always literally 0 for this reason.

    It is the cheapest detector there is for a mismapped column. Swapping
    foreign and institutional flows leaves every number individually plausible
    and every downstream feature quietly wrong — but the identity holds only for
    the correct assignment, so the swap shows up here immediately.
    """
    columns = ["foreign_net", "inst_net", "retail_net", "other_corp_net"]
    missing = [c for c in columns if c not in df.columns]
    if missing:
        return CheckResult("flow_identity", False, f"missing {missing}")
    if df.empty:
        return CheckResult("flow_identity", True, "no rows to check")

    totals = df[columns].sum(axis=1)
    broken = totals[totals != 0]
    if broken.empty:
        return CheckResult("flow_identity", True, f"{len(df)} rows sum to zero")

    worst = broken.abs().max()
    sample = df.loc[broken.index[:3], ["date", "ticker"]].to_dict("records")
    return CheckResult(
        "flow_identity",
        False,
        f"{len(broken)} of {len(df)} rows do not sum to zero (largest residual {worst:,}); "
        f"investor columns are probably mismapped — first: {sample}",
    )


def check_implied_close(df: pd.DataFrame) -> CheckResult:
    """market_cap ÷ shares_outstanding must equal the close kr_price recorded.

    Ties this collector to an independently confirmed number. kr_price's close
    for 005930 on 2024-01-02 was cross-checked against Naver Finance, so if the
    two derive different prices for the same session one of them is wrong and
    the report would carry both without noticing.
    """
    where, expected = _IMPLIED_CLOSE["where"], _IMPLIED_CLOSE["expected"]
    mask = pd.Series(True, index=df.index)
    for column, value in where.items():
        if column not in df.columns:
            return CheckResult("implied_close", False, f"column {column!r} is absent")
        series = df[column]
        if column == "date":
            series = pd.to_datetime(series).dt.date
        mask &= series == value

    rows = df[mask]
    if rows.empty:
        return CheckResult("implied_close", True, f"no row at {where}; nothing to check")

    row = rows.iloc[0]
    if not row["shares_outstanding"]:
        return CheckResult("implied_close", False, "shares_outstanding is zero")

    implied = row["market_cap"] / row["shares_outstanding"]
    if abs(implied - expected) < 1.0:
        return CheckResult(
            "implied_close", True, f"implied close {implied:,.0f} matches kr_price's {expected:,}"
        )
    return CheckResult(
        "implied_close",
        False,
        f"market_cap/shares implies {implied:,.2f} but kr_price records {expected:,} "
        f"for {where} — the two collectors disagree about the same session",
    )


def validate_frame(
    df: pd.DataFrame,
    tickers: Sequence[str],
    start: dt.date,
    end: dt.date,
    *,
    known_value: bool = True,
) -> ValidationReport:
    """Run every check against a fetched or reloaded frame.

    Continuity is per ticker: a frame holding several tickers has one row per
    ticker per session, and the aggregate would look duplicated.
    """
    checks: list[CheckResult] = [
        check_schema(df, SCHEMA),
        check_missing_ratio(df, MISSING_THRESHOLDS),
        check_flow_identity(df),
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
        checks.append(check_implied_close(df))

    return validate(COLLECTOR, checks)


# --- fetching ------------------------------------------------------------


def _frame(raw: pd.DataFrame, rename: dict[str, str]) -> pd.DataFrame:
    """Rename the columns this project uses and index by session date."""
    if raw is None or raw.empty:
        return pd.DataFrame()
    df = raw.rename(columns=rename).reset_index(names="date")
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[s]")
    keep = ["date"] + [c for c in rename.values() if c in df.columns]
    return df[keep]


def normalize(
    flow: pd.DataFrame,
    cap: pd.DataFrame,
    short: pd.DataFrame,
    fundamental: pd.DataFrame,
    ticker: str,
) -> pd.DataFrame:
    """Merge the four endpoints into one row per session.

    **Market cap is the spine, not flows.** A halted session appears in the cap
    and OHLCV endpoints with zero volume and an unchanged price, but is absent
    from the flow endpoint entirely — there were no trades, so there is no net
    purchase to report. Observed for 003680 on 2026-07-16, which hit the upper
    limit three sessions running, was suspended for a day, then hit the lower
    limit.

    Joining flows inner dropped that session, and that is not a cosmetic gap.
    ``foreign_flow_5d`` is a five-*session* window; silently removing a session
    turns it into "five sessions that happened to have flow data", sliding the
    window an extra day back and doing so only for the names that were halted —
    exactly the tickers whose flows are most worth reading.

    On a halted session the flows are filled with zero rather than left null,
    because zero is what actually happened: no trades means no net purchase.
    Where flows are missing but volume is *not* zero, the null is kept, since
    that is a real gap rather than a halt and should reach the missing-ratio
    check.

    Short interest and fundamentals are joined left for a different reason —
    KRX publishes no PER against negative earnings and no short balance for
    every name. Making those inner would delete otherwise-good sessions.
    """
    flow = _frame(flow, _RENAME_FLOW)
    cap = _frame(cap, _RENAME_CAP)
    if flow.empty or cap.empty:
        return pd.DataFrame(columns=list(SCHEMA))

    df = cap.merge(flow, on="date", how="left")

    # A session present in cap but absent from flows is a halt if nothing
    # traded. Zero is the correct value, and keeping the row is what keeps the
    # five-session flow windows aligned to sessions rather than to data.
    halted = df["foreign_net"].isna() & (df["volume"] == 0)
    for column in _RENAME_FLOW.values():
        df.loc[halted, column] = 0
    for extra, rename in ((short, _RENAME_SHORT), (fundamental, _RENAME_FUND)):
        frame = _frame(extra, rename)
        if not frame.empty:
            df = df.merge(frame, on="date", how="left")

    df["ticker"] = ticker
    for column in SCHEMA:
        if column not in df.columns:
            df[column] = pd.NA
    df["known_at_utc"] = [session_close_utc("KR", d.date()) for d in df["date"]]
    df = df[list(SCHEMA)]
    # Cast here rather than in fetch. This function is what produces the
    # committed schema, so it is what must satisfy it — otherwise check_schema
    # passes for a fetched frame and fails for the identical reloaded one.
    # Nullable Int64 matters: a left join leaves NA where short interest or
    # fundamentals were absent, and numpy int64 cannot hold it.
    df = df.astype({k: v for k, v in SCHEMA.items() if k != "known_at_utc"})
    df["known_at_utc"] = pd.to_datetime(df["known_at_utc"], utc=True)
    return df


def fetch(
    tickers: Iterable[str],
    start: dt.date,
    end: dt.date,
    *,
    as_of: pd.Timestamp | None = None,
    sleep_seconds: float = _SLEEP_SECONDS,
) -> tuple[pd.DataFrame, ValidationReport]:
    """Fetch flows, short interest, cap and fundamentals over ``[start, end]``.

    ``as_of`` is the look-ahead boundary: any row whose ``known_at_utc`` is at
    or after it is dropped. ``None`` means no boundary and suits only a
    historical backfill, where the boundary is applied at feature-computation
    time instead.

    Returns the frame and its report rather than raising, so a failing collector
    records the failure and lets the pipeline publish a partial report.
    """
    tickers = list(tickers)

    try:
        stock = import_pykrx_stock()
    except KrxSessionError as exc:
        # Reported, not raised: CLAUDE.md requires a failing collector to let the
        # pipeline publish a partial report. See src/util/krx.py for why this
        # surfaces at import time at all.
        report = validate_frame(
            pd.DataFrame(columns=list(SCHEMA)), tickers, start, end, known_value=False
        )
        report.add(CheckResult("krx_session", False, str(exc)))
        return pd.DataFrame(columns=list(SCHEMA)), report
    frames: list[pd.DataFrame] = []
    failures: list[str] = []
    from_, to_ = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

    def call(fn, first: bool) -> pd.DataFrame:
        """One scraped request, preceded by the sleep SPEC §3.1 requires."""
        if not first:
            time.sleep(sleep_seconds)
        return fn(from_, to_, ticker)

    for index, ticker in enumerate(tickers):
        merged = pd.DataFrame()
        for attempt in range(_RETRIES + 1):
            if attempt:
                # Backing off further than the normal cadence: arriving at the
                # same rate that triggered the block would just be blocked again.
                time.sleep(_RETRY_BACKOFF)
            flow = call(stock.get_market_trading_value_by_date, first=(index == 0 and not attempt))
            cap = call(stock.get_market_cap_by_date, first=False)
            short = call(stock.get_shorting_balance_by_date, first=False)
            fundamental = call(stock.get_market_fundamental_by_date, first=False)

            merged = normalize(flow, cap, short, fundamental, ticker)
            if not merged.empty:
                break

        if merged.empty:
            failures.append(ticker)
            continue
        frames.append(merged)

    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=list(SCHEMA))

    if not df.empty:
        # concat can widen a dtype when frames differ, so the schema is
        # reasserted after the join rather than trusted through it.
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
