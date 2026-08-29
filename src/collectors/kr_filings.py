"""DART OpenAPI filings. SPEC §2.2② `filing` flag and §3.2.

Verified live on 2026-08-25 against DART's DS001 (공시정보) group, the gap
``notes/calendar-collector-plan.md`` (2026-08-14) explicitly left open — that
plan read DS002 (dividend facts) and DS006 (equity-securities registration)
for the calendar collector's different purpose, but never DS001, which is
where the actual filing-list endpoint lives.

**Endpoint (공시검색, apiId 2019001):**
``GET https://opendart.fss.or.kr/api/list.json`` — params ``crtfc_key``,
``corp_code`` (8-digit, single company per call — there is no multi-corp_code
parameter, so this collector loops per company, the same shape ``kr_flow.py``
uses for its own per-ticker pykrx calls), ``bgn_de``/``end_de`` (``YYYYMMDD``;
capped to 3 months when ``corp_code`` is absent — not a constraint this
collector hits, since it always passes one), ``page_no``/``page_count`` (max
100/page — a company can have more than 100 filings inside a long window, so
`fetch` paginates via ``total_page``). Confirmed response fields, from a live
call against Samsung Electronics (corp_code ``00126380``, 2026-07-01 to
2026-08-25, 832 total rows): ``rcept_no``, ``corp_cls``, ``corp_name``,
``corp_code``, ``stock_code``, ``report_nm``, ``rcept_dt``, ``flr_nm``, ``rm``.

**``rcept_dt`` is date-only** (``YYYYMMDD``, no time component) — unlike SEC's
``acceptanceDateTime``, DART gives no intraday timestamp. ``known_at_utc``
therefore uses the same conservative derivation ``kr_flow.py`` uses for its
own date-only KRX data: the filing is knowable no earlier than the close of
its own filing-date session (``session_close_utc("KR", rcept_dt)``), since a
same-day filing can legally land right up to a deadline.

**Rate limit: 20,000 calls/day**, confirmed 2026-08-25 from DART's own status
code table (``status="020"`` is literally defined as the over-limit response;
``status="013"`` is "no data found", handled below as a normal empty result,
not an error). 31 KR tickers × 2 runs/day is trivially inside it.

**Filing-type scope is unfiltered, and the live Samsung pull shows why that
is not a free choice.** 832 filings in 55 days for one company — mostly
routine 임원·주요주주 ownership reports and related-party transaction
disclosures — means an unfiltered `filing` flag will fire on most sessions
for a high-filing-frequency name like Samsung. This is a measured finding
from the live verification call, not speculation, and is recorded here and
in ``notes/filings-collector-plan.md`` as the concrete evidence a v2 type
filter should be built against, rather than left as an abstract "might be
noisy" concern.

**Storage is parquet.** See ``us_filings.py``'s module docstring for the full
reasoning — the same deviation from SPEC §3.3's literal ``.jsonl`` line
applies to both markets, and SPEC §3.3 does not name a ``kr/filings/`` path
at all, which this collector's storage wiring now supplies.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from collections.abc import Iterable

import pandas as pd
import requests

from src.collectors.validate import (
    CheckResult,
    ValidationReport,
    check_known_row_exists,
    check_missing_ratio,
    check_schema,
    empty_frame,
    validate,
)
from src.util.session import NoSessionFoundError, next_tradeable_open, session_close_utc, to_utc

COLLECTOR = "kr_filings"

_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
_PAGE_COUNT = 100

SCHEMA = {
    "corp_code": "object",  # DART's 8-digit company id, not the KRX ticker
    "ticker": "object",
    "rcept_no": "object",
    "report_nm": "object",
    "flr_nm": "object",  # filer name — nullable, DART leaves it blank on some rows
    "date": "datetime64[s]",
    "known_at_utc": "datetime64[ns, UTC]",
}

MISSING_THRESHOLDS = {
    "corp_code": 0.0,
    "ticker": 0.0,
    "rcept_no": 0.0,
    "report_nm": 0.0,
    "date": 0.0,
    "known_at_utc": 0.0,
    "flr_nm": 0.1,
}

# Samsung Electronics' 2026 half-year report, filed exactly on the statutory
# deadline (period end 2026-06-30 + 45 days = 2026-08-14) — cross-checked
# 2026-08-25 against that deadline calculation rather than against DART
# itself, since a self-consistency check against the API under test would
# prove nothing.
KNOWN_VALUE = {
    "where": {"rcept_no": "20260814003699", "ticker": "005930"},
}

# Observed transfer characteristics differ sharply between corpCode.xml (slow,
# large, one-time) and list.json (fast, small, per-company) — this sleep
# paces the per-company loop, not the one-off id-resolution download.
_SLEEP_SECONDS = 0.5
_RETRIES = 1
_RETRY_BACKOFF = 3.0


class DartFilingsError(RuntimeError):
    """Raised when ``DART_API_KEY`` is missing — never for a per-company HTTP failure."""


# --- validation --------------------------------------------------------


def check_filing_plausibility(
    df: pd.DataFrame, corp_codes: list[str], start: dt.date, end: dt.date
) -> CheckResult:
    """Mirrors ``us_filings.check_filing_plausibility`` — see its docstring for
    why this replaces strict trading-day continuity rather than a non-empty
    check: zero filings for a company in a short window is the normal case.
    """
    if df.empty:
        return CheckResult("filing_plausibility", True, "no rows; nothing to check")

    problems: list[str] = []

    dates = pd.to_datetime(df["date"]).dt.date
    out_of_range = dates[(dates < start) | (dates > end)]
    if not out_of_range.empty:
        problems.append(f"{len(out_of_range)} date values outside [{start}, {end}]")

    known_at = pd.to_datetime(df["known_at_utc"], utc=True)
    filed_at = pd.to_datetime(dates, utc=True)
    stale = known_at <= filed_at
    if stale.any():
        problems.append(f"{int(stale.sum())} rows have known_at_utc <= date")

    duplicated = df["rcept_no"].duplicated().sum()
    if duplicated:
        problems.append(f"{duplicated} duplicated rcept_no values")

    unknown = set(df["corp_code"]) - set(corp_codes)
    if unknown:
        problems.append(f"rows for unrequested corp_code(s): {sorted(unknown)}")

    if problems:
        return CheckResult("filing_plausibility", False, "; ".join(problems))
    return CheckResult("filing_plausibility", True, f"{len(df)} rows plausible")


def validate_frame(
    df: pd.DataFrame,
    corp_codes: list[str],
    start: dt.date,
    end: dt.date,
    *,
    known_value: bool = True,
) -> ValidationReport:
    checks = [
        check_schema(df, SCHEMA),
        # Same override kr_news.validate_frame applies to its own zero-row
        # case — check_missing_ratio treats empty as a failure, which is
        # wrong here: zero filings for a company in a short window is normal.
        check_missing_ratio(df, MISSING_THRESHOLDS)
        if len(df)
        else CheckResult("missing_ratio", True, "no rows"),
        check_filing_plausibility(df, corp_codes, start, end),
    ]
    if known_value:
        checks.append(check_known_row_exists(df, KNOWN_VALUE["where"]))
    return validate(COLLECTOR, checks)


# --- fetching ------------------------------------------------------------


def _known_at(date: dt.date) -> pd.Timestamp:
    """Conservative timestamp for DART's date-only filing record.

    DART can accept a filing on a KRX closure. On a normal session the existing
    close-of-session convention remains; on a closure the next tradable open is
    the earliest safe timestamp and, unlike a fabricated close, exists.
    """
    try:
        return session_close_utc("KR", date)
    except NoSessionFoundError:
        return next_tradeable_open("KR", pd.Timestamp(date, tz="UTC"))


def _parse(rows: list[dict], ticker: str) -> pd.DataFrame:
    if not rows:
        return empty_frame(SCHEMA)

    df = pd.DataFrame(rows)
    df["ticker"] = ticker
    df["date"] = pd.to_datetime(df["rcept_dt"], format="%Y%m%d")
    df["flr_nm"] = df["flr_nm"].replace("", None)
    df["known_at_utc"] = [_known_at(d.date()) for d in df["date"]]
    df = df[list(SCHEMA)]
    # Cast here, not in fetch — the same reasoning as us_filings._parse: this
    # is what produces the committed schema, so check_schema must pass on
    # both the fresh fetch and the reloaded parquet.
    df = df.astype({k: v for k, v in SCHEMA.items() if k != "known_at_utc"})
    df["known_at_utc"] = pd.to_datetime(df["known_at_utc"], utc=True)
    return df


def _fetch_one(
    corp_code: str, start: dt.date, end: dt.date, api_key: str, *, sleep_seconds: float
) -> list[dict]:
    """All pages for one company. Raises on an HTTP failure — the retry lives
    in :func:`fetch`, which also owns the sleep budget before each company's
    first page; this function sleeps only *between* its own pages."""
    rows: list[dict] = []
    page = 1
    while True:
        if page > 1:
            time.sleep(sleep_seconds)
        response = requests.get(
            _LIST_URL,
            params={
                "crtfc_key": api_key,
                "corp_code": corp_code,
                "bgn_de": start.strftime("%Y%m%d"),
                "end_de": end.strftime("%Y%m%d"),
                "page_no": page,
                "page_count": _PAGE_COUNT,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        status = payload.get("status")
        if status == "013":  # DART's own "no data found" status — not an error
            break
        if status != "000":
            raise requests.HTTPError(f"DART status {status}: {payload.get('message')}")
        rows.extend(payload.get("list", []))
        if page >= int(payload.get("total_page", 1)):
            break
        page += 1
    return rows


def fetch(
    tickers: Iterable[str],
    corp_code_by_ticker: dict[str, dict[str, str]],
    start: dt.date,
    end: dt.date,
    *,
    as_of: pd.Timestamp | None = None,
    api_key: str | None = None,
    sleep_seconds: float = _SLEEP_SECONDS,
) -> tuple[pd.DataFrame, ValidationReport]:
    """Fetch each ticker's DART filings and filter to ``[start, end]``.

    ``corp_code_by_ticker`` is ``load_filing_ids()["kr"]`` — resolved by the
    caller, not loaded here, the same pattern ``us_filings.fetch`` uses and
    ``collect_daily.py`` already applies to ``kr_news.fetch``.

    One bounded retry per company on a transient HTTP failure, matching
    ``kr_flow.fetch``'s shape — DART's block is a daily-quota concept rather
    than KRX's session-based one, but a single genuinely-down request should
    still be retried once rather than immediately counted as a failure.
    Never raises on a per-company failure; a missing ``api_key`` raises
    immediately, the same configuration-vs-per-item distinction
    ``us_filings.fetch`` makes for ``SEC_USER_AGENT``.
    """
    api_key = api_key or os.environ.get("DART_API_KEY")
    if not api_key:
        raise DartFilingsError("DART_API_KEY is not set")

    tickers = list(tickers)
    frames: list[pd.DataFrame] = []
    failures: list[str] = []
    corp_codes: list[str] = []

    for index, ticker in enumerate(tickers):
        entry = corp_code_by_ticker.get(ticker)
        if entry is None:
            failures.append(f"{ticker}: no corp_code in config/filing_ids.yaml")
            continue
        corp_code = entry["corp_code"]
        corp_codes.append(corp_code)

        rows: list[dict] = []
        last_error: Exception | None = None
        for attempt in range(_RETRIES + 1):
            if attempt:
                time.sleep(_RETRY_BACKOFF)
            elif index:
                time.sleep(sleep_seconds)
            try:
                rows = _fetch_one(corp_code, start, end, api_key, sleep_seconds=sleep_seconds)
                last_error = None
                break
            except requests.RequestException as exc:
                last_error = exc

        if last_error is not None:
            failures.append(f"{ticker}: {last_error}")
            continue

        parsed = _parse(rows, ticker)
        if not parsed.empty:
            frames.append(parsed)

    df = pd.concat(frames, ignore_index=True) if frames else empty_frame(SCHEMA)
    if not df.empty:
        df = df.astype({k: v for k, v in SCHEMA.items() if k != "known_at_utc"})
        df["known_at_utc"] = pd.to_datetime(df["known_at_utc"], utc=True)
        if as_of is not None:
            df = df[df["known_at_utc"] < to_utc(as_of)].reset_index(drop=True)

    report = validate_frame(df, corp_codes, start, end, known_value=False)
    if failures:
        report.add(CheckResult("fetch", False, f"{len(failures)} ticker(s) failed: {failures}"))
    return df, report
