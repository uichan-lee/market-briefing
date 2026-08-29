"""SEC EDGAR filings. SPEC §2.2② `filing` flag and §3.2.

Verified live on 2026-08-25 against SEC's submissions API rather than assumed,
per CLAUDE.md's UNVERIFIED-marker discipline — the ticker-lookup call that
proved `SEC_USER_AGENT` works (MANUAL-TASKS.md §0, 2026-08-04) never exercised
the filing-list endpoint itself.

**Endpoint and shape.** ``GET https://data.sec.gov/submissions/CIK{cik}.json``
returns one JSON document per company. ``filings.recent`` is **columnar** —
parallel arrays keyed by field name, one index per filing, not a list of row
objects — capped at the most recent 1000 entries; older filings live in
``filings.files``, a list of paginated ``CIK{cik}-submissions-NNN.json``
documents this collector does not read (see the limitation note on
:func:`fetch`). Confirmed field names, from a live call against Apple
(CIK 0000320193, 1001 rows): ``accessionNumber``, ``filingDate``,
``reportDate``, ``acceptanceDateTime``, ``form``, ``primaryDocument``.

**`acceptanceDateTime` is a real intraday UTC timestamp** (e.g.
``"2026-08-20T22:30:16.000Z"``), present on all 1001 rows of the live sample —
a materially better ``known_at_utc`` source than the conservative
next-session-open derivation this repo's other collectors fall back to when a
source publishes date-only. It is used directly, with the next-session-open
derivation kept only as a defensive fallback for a row that lacks it (untested
against a real gap, since none appeared in the live sample).

**`reportDate` is legitimately blank for filings with no reporting period**
(Form 144, Form 4 without one, etc.) — 23% empty in the single-company Apple
sample, but **93.3% empty measured 2026-08-25 across the full 40-ticker US
watchlist** (1550 rows, 8-day window): Apple's own filing mix is unusually
report-period-heavy compared to the watchlist as a whole, which is mostly
Form 4/144/424B2/FWP — the single-company number was not representative and
the threshold below is calibrated on the real watchlist measurement, not the
sample that motivated the column. ``primaryDocument`` was 0% empty in both.

**`acceptanceDateTime` can be *before* midnight UTC of `date` — this is SEC's
own rule, not a defect.** EDGAR assigns a filing's regulatory ``filingDate``
as the next business day for anything accepted after its 5:30pm ET cutoff, so
a filing accepted at e.g. 19:44 UTC (~3:44pm ET) with ``date`` one calendar
day later is correct data, not a look-ahead violation — measured 2026-08-25
on JPMorgan's 424B2/FWP filings (75 of 1550 rows across the full watchlist).
``known_at_utc`` is still right in this case: it already carries the real
``acceptanceDateTime``, the actual moment the filing became public, which is
the number that matters for the look-ahead boundary. ``date`` is a separate,
regulatory bookkeeping label with no ordering guarantee against it, so
``check_filing_plausibility`` does not assert one — seeing this fire on real
data before the window closed is what caught the wrong assumption.

**Filing-type scope is unfiltered in this version.** SPEC §2.2② states `filing`
as a bare presence flag with no type filter, and `filing` carries no
``rating.yaml`` weight — it is a display-only flag like ``volatility_z`` in
``render.py``, not a rating input. A live DART pull for Samsung (see
``kr_filings.py``) showed this will make the flag fire on most sessions for a
high-filing-frequency company; that is now a measured finding, not a guess,
and is left for a v2 type filter rather than designed around speculatively —
see ``notes/filings-collector-plan.md``.

**Storage is parquet, not the ``.jsonl`` SPEC §3.3 names.** Every load-bearing
integration point this collector plugs into — ``write_daily``/``backfill.py``'s
versioning, ``features/compute.py``'s ``load_raw`` — is parquet-only. Building
a second JSONL writer for one collector would duplicate that machinery for no
benefit. Recorded as a deviation in ``notes/filings-collector-plan.md``; SPEC
§3.3 is corrected to match rather than left to silently disagree with the code,
per the precedent ``notes/calendar-collector-plan.md`` set for §2.2④.
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
    check_known_row_exists,
    check_missing_ratio,
    check_schema,
    empty_frame,
    validate,
)
from src.util.session import NoSessionFoundError, next_tradeable_open, session_close_utc, to_utc

COLLECTOR = "us_filings"

_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

SCHEMA = {
    "cik": "object",  # 10-digit, zero-padded — SEC's own CIK string form.
    "ticker": "object",
    "accession_no": "object",
    "form": "object",
    "date": "datetime64[s]",
    # Blank for filings with no reporting period (Form 144, some Form 4s) —
    # 23% of a live 1001-row Apple sample. Nullable, not a collector fault.
    "report_date": "datetime64[s]",
    "primary_document": "object",
    "known_at_utc": "datetime64[ns, UTC]",
}

MISSING_THRESHOLDS = {
    "cik": 0.0,
    "ticker": 0.0,
    "accession_no": 0.0,
    "form": 0.0,
    "date": 0.0,
    "known_at_utc": 0.0,
    # 93.3% missing measured 2026-08-25 across the full watchlist (see module
    # docstring) — not a data-quality signal, just this watchlist's filing
    # mix. 0.97 leaves margin without disabling the check entirely.
    "report_date": 0.97,
    "primary_document": 0.02,  # 0% missing in the live sample; near-zero, not 0
}

# Apple's FY2025 10-K, cross-checked 2026-08-25 against three independent
# sources (last10k.com, fintel.io, TradingView — none is the EDGAR API under
# test) rather than pinned from memory: filed 2025-10-31, accession
# 0000320193-25-000079.
KNOWN_VALUE = {
    "where": {"accession_no": "0000320193-25-000079", "ticker": "AAPL"},
}


class SecFilingsError(RuntimeError):
    """Raised when ``SEC_USER_AGENT`` is missing — never for a per-CIK HTTP failure."""


def _user_agent() -> str:
    agent = os.environ.get("SEC_USER_AGENT")
    if not agent:
        raise SecFilingsError(
            "SEC_USER_AGENT is not set. SEC rejects requests without a descriptive "
            "User-Agent (format: 'market-briefing <contact email>')."
        )
    return agent


# --- validation ------------------------------------------------------------


def check_filing_plausibility(
    df: pd.DataFrame, ciks: Sequence[str], start: dt.date, end: dt.date
) -> CheckResult:
    """Replaces strict trading-day continuity, which does not fit event data.

    A filing is not one-row-per-session — a company files zero or several
    times a day, and **zero inside a short fetch window is the normal case**,
    the same "a quiet run is not a failure" principle CLAUDE.md states for
    ``kr_news``'s zero-row files, applied here for the first time to a
    non-news source. So this checks the *plausibility* of whatever rows came
    back, never non-emptiness: every ``date`` falls inside
    ``[start, end]``, ``known_at_utc`` is within a sane distance of ``date``
    (see below — not a strict ordering, which real data violates),
    ``accession_no`` values are unique (catches duplicate ingestion), and
    every ``cik`` is one of the ones requested (catches a swapped identifier).

    ``known_at_utc`` is *not* required to be after ``date``. SEC assigns a
    filing's regulatory ``date`` as the next *business* day for anything
    accepted after its 5:30pm ET cutoff, so a real ``acceptanceDateTime`` can
    land before midnight UTC of ``date`` by more than a calendar day —
    measured 2026-08-25 on 75 of 1550 real rows (JPMorgan 424B2/FWP filings
    accepted on an ordinary weekday) and, separately, by up to ~3 days on 14
    more rows where the acceptance fell on a Friday evening and the next
    business day was the following Monday (JPM/BAC/GS/MS, same run). A first
    version of this check bounded the gap at 1 day and failed on the first
    75; a second at 1 day undercounted the weekend case and failed on 14
    more. What this checks is therefore a wide bound (``[date - 5d,
    date + 5d]``) rather than a precise business-day derivation — precise
    would mean re-implementing SEC's own holiday calendar, and a plausibility
    check exists to catch a genuinely wrong value (a year off), not to
    re-derive EDGAR's assignment rule exactly.
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
    implausible = (known_at < filed_at - pd.Timedelta(days=5)) | (
        known_at > filed_at + pd.Timedelta(days=5)
    )
    if implausible.any():
        problems.append(
            f"{int(implausible.sum())} rows have known_at_utc implausibly far from date"
        )

    duplicated = df["accession_no"].duplicated().sum()
    if duplicated:
        problems.append(f"{duplicated} duplicated accession_no values")

    unknown_cik = set(df["cik"]) - set(ciks)
    if unknown_cik:
        problems.append(f"rows for unrequested CIK(s): {sorted(unknown_cik)}")

    if problems:
        return CheckResult("filing_plausibility", False, "; ".join(problems))
    return CheckResult("filing_plausibility", True, f"{len(df)} rows plausible")


def validate_frame(
    df: pd.DataFrame,
    ciks: Sequence[str],
    start: dt.date,
    end: dt.date,
    *,
    known_value: bool = True,
) -> ValidationReport:
    checks = [
        check_schema(df, SCHEMA),
        # check_missing_ratio treats an empty frame as a failure (right for
        # every other collector, where empty means the fetch broke — wrong
        # here, where empty over a short window is normal). Same override
        # kr_news.validate_frame applies to its own zero-row case.
        check_missing_ratio(df, MISSING_THRESHOLDS)
        if len(df)
        else CheckResult("missing_ratio", True, "no rows"),
        check_filing_plausibility(df, ciks, start, end),
    ]
    if known_value:
        checks.append(check_known_row_exists(df, KNOWN_VALUE["where"]))
    return validate(COLLECTOR, checks)


# --- fetching ---------------------------------------------------------------


def _fallback_known_at(date: dt.date) -> pd.Timestamp:
    """Safe fallback when EDGAR omits its intraday acceptance timestamp."""
    try:
        return session_close_utc("US", date)
    except NoSessionFoundError:
        return next_tradeable_open("US", pd.Timestamp(date, tz="UTC"))


def _parse(payload: dict, ticker: str) -> pd.DataFrame:
    """Turn one company's ``filings.recent`` columnar block into row form."""
    recent = payload.get("filings", {}).get("recent", {})
    if not recent:
        return empty_frame(SCHEMA)

    cik = str(payload["cik"]).zfill(10)
    n = len(recent.get("accessionNumber", []))

    def col(name: str) -> list:
        values = recent.get(name, [])
        return list(values) + [None] * (n - len(values))

    accept = col("acceptanceDateTime")
    date = col("filingDate")
    known_at = [
        to_utc(pd.Timestamp(a)) if a else _fallback_known_at(dt.date.fromisoformat(d))
        for a, d in zip(accept, date, strict=True)
    ]

    df = pd.DataFrame(
        {
            "cik": [cik] * n,
            "ticker": [ticker] * n,
            "accession_no": col("accessionNumber"),
            "form": col("form"),
            "date": pd.to_datetime(date),
            "report_date": pd.to_datetime([d or None for d in col("reportDate")]),
            "primary_document": [d or None for d in col("primaryDocument")],
            "known_at_utc": known_at,
        }
    )
    # Cast here, not in fetch — this is what produces the committed schema, so
    # it is what must satisfy it, for the identical reason kr_flow.normalize
    # states: otherwise check_schema passes for a fresh fetch and fails for
    # the reloaded parquet.
    df = df.astype({k: v for k, v in SCHEMA.items() if k != "known_at_utc"})
    df["known_at_utc"] = pd.to_datetime(df["known_at_utc"], utc=True)
    return df


def fetch(
    tickers: Iterable[str],
    cik_by_ticker: dict[str, dict[str, str]],
    start: dt.date,
    end: dt.date,
    *,
    as_of: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, ValidationReport]:
    """Fetch each ticker's recent SEC filings and filter to ``[start, end]``.

    ``cik_by_ticker`` is ``load_filing_ids()["us"]`` — resolved by the caller,
    not loaded here, the same pattern ``collect_daily.py`` already uses for
    ``kr_news.fetch(load_news_feeds(), ...)``: config loading happens at the
    call site, so this function stays testable against a synthetic mapping
    instead of the real ``config/filing_ids.yaml``.

    **Reads only ``filings.recent`` (the most recent ~1000 filings per
    company) — it does not paginate into ``filings.files``.** That is correct
    for the daily driver, whose window is always a few days wide, but it means
    a multi-year backfill against a high-volume filer (Apple files roughly
    600 Form 4s in a few months) will silently stop short of full history
    unless pagination is added first. Stated here rather than discovered
    later: ``scripts/backfill.py``'s ``backfill_us_filings`` must not be
    trusted for windows this collector cannot actually reach.

    Never raises on a per-ticker failure — CLAUDE.md requires a failing
    collector to record the failure and let the pipeline publish a partial
    report, so one company's HTTP error is recorded in ``fetch`` and the rest
    still return.
    """
    tickers = list(tickers)
    headers = {"User-Agent": _user_agent()}

    frames: list[pd.DataFrame] = []
    failures: list[str] = []
    ciks: list[str] = []

    for ticker in tickers:
        entry = cik_by_ticker.get(ticker)
        if entry is None:
            failures.append(f"{ticker}: no CIK in config/filing_ids.yaml")
            continue
        cik = str(entry["cik"]).zfill(10)
        ciks.append(cik)
        url = _SUBMISSIONS_URL.format(cik=cik)
        try:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            failures.append(f"{ticker}: {exc}")
            continue

        parsed = _parse(payload, ticker)
        dates = pd.to_datetime(parsed["date"]).dt.date
        parsed = parsed[(dates >= start) & (dates <= end)].reset_index(drop=True)
        if not parsed.empty:
            frames.append(parsed)

    df = pd.concat(frames, ignore_index=True) if frames else empty_frame(SCHEMA)
    if not df.empty:
        df = df.astype({k: v for k, v in SCHEMA.items() if k != "known_at_utc"})
        df["known_at_utc"] = pd.to_datetime(df["known_at_utc"], utc=True)
        if as_of is not None:
            df = df[df["known_at_utc"] < to_utc(as_of)].reset_index(drop=True)

    report = validate_frame(df, ciks, start, end, known_value=False)
    if failures:
        report.add(CheckResult("fetch", False, f"{len(failures)} ticker(s) failed: {failures}"))
    return df, report
