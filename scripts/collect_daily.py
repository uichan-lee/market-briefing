"""Collect one scheduled run's data. SPEC §1, §12 step 11.

    uv run python -m scripts.collect_daily --run evening

Two runs a day, and they collect different things because their constraints
differ (notes/step11-plan.md):

* ``evening`` (21:37 KST) — the KR session closed at 15:30 and is final, and the
  previous US session is behind the Alpaca SIP recency boundary. Collects
  ``kr_price``, ``kr_flow``, ``macro``, and canonical ``us_price`` via Alpaca.
* ``morning`` (07:07 KST) — **no KRX call.** The KR session being reported on
  has not opened, and ``kr_flow`` costs 124 KRX requests per run against a block
  observed near 250. Collects the just-closed US session via Tiingo into
  ``data/raw/us/price_preview/`` — a display-only path the feature pipeline
  never reads — plus a ``macro`` refresh.

Both runs also collect ``kr_news`` and ``calendar``: the hourly workflow
already covers the news clock, but an extra poll costs nothing (dedup drops
what is already stored) and it is what produces a *fresh* feed-continuity
check for the report header. ``calendar`` (SPEC §2.2④, partial — macro
release dates and options expiry) has no KRX dependency and nothing that
favors one run over the other, so it runs in both, same as ``macro``.

**The whole catch-up window is re-fetched every run, deliberately.** Some of
this data publishes late — KRX short-sale balance lags the session by two days,
WTI on FRED by up to four — so a date that was already written can gain rows
afterwards. Skipping already-written dates (what the backfill does) would freeze
those gaps permanently. Instead each date is compared against what is stored and
written to a ``-vN`` path only when it actually changed, which satisfies
CLAUDE.md rule 1 with no overwrites. For KRX the re-fetch is free: its request
cost is per ticker per endpoint, independent of the date range.

A collector that fails is recorded and the others still run; the exit code is
zero as long as the run produced a status file, because the report must publish
partially rather than not at all. The status file under ``data/status/`` is how
check failures and news gaps reach the report header — logs are not enough.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pandas as pd

from src.collectors import (
    calendar as calendar_events,
)
from src.collectors import (
    kr_filings,
    kr_flow,
    kr_index,
    kr_news,
    kr_price,
    macro,
    us_filings,
    us_price,
    us_price_alpaca,
)
from src.collectors.validate import ValidationReport
from src.llm import daily_scoring
from src.util.config import load_filing_ids, load_news_feeds, load_watchlist
from src.util.session import now_utc

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RAW = ROOT / "data" / "raw"
STATUS = ROOT / "data" / "status"

# Calendar days each run looks back over. Wide enough that a few dropped
# Actions runs (observed: three of four) cost nothing, narrow enough that the
# nightly re-fetch stays one request per ticker per endpoint for KRX.
WINDOW_DAYS = 8

# Macro looks back further than everything else, because two of its six series
# publish on their own slower cadence. Measured 2026-08-09: `us_10y` had reached
# 2026-08-07 while `usdkrw` and `dollar_index` — the Federal Reserve FX series
# DEXKOUS and DTWEXBGS — both stopped at 2026-07-31, roughly a week behind.
#
# Against WINDOW_DAYS that produced a failure with no data problem behind it.
# The morning run fires at 22:07–23:23 UTC, so its `end` is the *previous* UTC
# day relative to the KST date it is read on: 2026-08-09 (Sun) gave the window
# 08-01..08-09, and 07-31 fell one day outside it. `check_coverage` cannot tell
# "published before my window" from "dead", so it reported `usdkrw has no rows
# at all` and the briefing header carried a 07-31 rate. Not a one-off — every
# Monday morning run lands the same way, since its `end` is always Sunday and
# the last FX observation is always the Friday before the window opens.
#
# The false alarm was the cheap half. The expensive half is that a window this
# short also *loses* data: `wti` is an EIA series FRED redistributes days
# behind, and 2026-07-28 published after 08-05, by which point the 8-day window
# had moved past it. Nothing re-fetched it, so the stored history carried a hole
# on a day FRED serves a value for (80.91, confirmed live 2026-08-10). It is the
# only genuine gap in three years of stored macro — every other one is Columbus
# Day or Veterans Day, when the bond market is shut and NYSE is not.
#
# So the window has to outlast the slowest publisher, not the fastest. A check
# that fires is recoverable; a window that closes before the data arrives is not.
#
# 30 covers a weekly cadence with room to spare, and the number does not depend
# on knowing the exact one — the FRED H.10 release schedule was NOT read in the
# session that set this, only the observed lag above. Widening WINDOW_DAYS
# globally instead would be the wrong fix: kr_flow spends 124 KRX requests
# against a block observed near 250.
#
# Verified live on 2026-08-10 against end=2026-08-09, the failing run's window:
# 8 days gave `dollar_index has no rows at all; usdkrw has no rows at all`,
# 30 gave all six series at 100% with stale tails of 1–5 trading days.
MACRO_WINDOW_DAYS = 30

# calendar_events isn't compensating for a slow publisher the way
# MACRO_WINDOW_DAYS is — CPI/employment/FOMC dates are announced months
# ahead, and options expiry is computed with no lag at all. This sizes how
# far forward the §2.2④ section needs to see, not how far back a fetch has
# to reach to catch a late publisher. 30 days back is a short trailing
# history for check_event_continuity; 120 days ahead covers a full quarter
# of scheduled releases and meetings (notes/calendar-collector-plan.md).
CALENDAR_LOOKBACK_DAYS = 30
CALENDAR_LOOKAHEAD_DAYS = 120

PATHS = {
    "kr_price": RAW / "kr" / "price",
    "kr_flow": RAW / "kr" / "investor_flow",
    "kr_index": RAW / "kr" / "benchmark",
    "us_price": RAW / "us" / "price",
    "us_price_preview": RAW / "us" / "price_preview",
    "macro": RAW / "macro",
    "calendar": RAW / "calendar",
    "us_filings": RAW / "us" / "filings",
    "kr_filings": RAW / "kr" / "filings",
}

# Row identity per source, for the changed-content comparison.
KEYS = {
    "kr_price": ["date", "ticker"],
    "kr_flow": ["date", "ticker"],
    "kr_index": ["date", "ticker"],
    "us_price": ["date", "ticker"],
    "us_price_preview": ["date", "ticker"],
    "macro": ["date", "series"],
    "calendar": ["event", "date"],
    # A ticker can file more than once on the same day, so the filing id
    # (not the date) is what makes a row unique.
    "us_filings": ["accession_no", "ticker"],
    "kr_filings": ["rcept_no", "ticker"],
}


def _latest_version(directory: Path, day: dt.date) -> Path | None:
    """The newest stored file for ``day`` — the ``-vN`` with the highest N."""
    base = directory / f"{day.isoformat()}.parquet"
    if not base.exists():
        return None
    version = 2
    newest = base
    while (candidate := directory / f"{day.isoformat()}-v{version}.parquet").exists():
        newest = candidate
        version += 1
    return newest


def _differs(stored: pd.DataFrame, fetched: pd.DataFrame, key: list[str]) -> bool:
    """Whether ``fetched`` says anything *more* than ``stored`` does.

    Three outcomes, and the asymmetry is the point:

    * fetched has rows stored lacks, or common rows changed → revise.
    * identical → nothing.
    * **stored has rows fetched lacks → nothing.** A shrunken fetch is a fetch
      that failed partway, not new information. Hit live on 2026-08-06: two
      preview runs inside one Tiingo rate-limit hour, the second got 23 of 48
      tickers before the 429, and without this rule those 23 rows were minted
      as a ``-vN`` revision of a complete 48-row file. The validation report
      already states the fetch problem; the dataset must not record it as a
      correction.

    Compared on **values, not dtypes**. The stored side has been through a
    parquet round trip, which is not dtype-faithful — ``datetime64[s]`` comes
    back ``datetime64[ms]`` — and ``DataFrame.equals`` is dtype-strict. With a
    strict compare every run minted a fresh ``-vN`` of identical content;
    measured live on 2026-08-06, macro reached ``-v4`` inside ten minutes.
    """
    stored_keys = {tuple(row) for row in stored[key].itertuples(index=False)}
    fetched_keys = {tuple(row) for row in fetched[key].itertuples(index=False)}
    if stored_keys - fetched_keys:
        return False  # shrinkage: the fetch knows less than the store
    if fetched_keys - stored_keys:
        return True
    if set(stored.columns) != set(fetched.columns):
        return True
    # `known_at_utc` records when this particular collection run learned an
    # event. It is provenance, not an event revision: comparing it would mint
    # a new immutable raw file on every refetch of otherwise identical rows.
    columns = sorted(column for column in stored.columns if column != "known_at_utc")
    left = stored.sort_values(key)[columns].reset_index(drop=True)
    right = fetched.sort_values(key)[columns].reset_index(drop=True)
    for column in columns:
        if pd.api.types.is_datetime64_any_dtype(left[column]):
            # Parquet may restore datetime64[s] as datetime64[ms]. Convert both
            # sides to one UTC representation before comparing, including NaT.
            left[column] = pd.to_datetime(left[column], utc=True)
            right[column] = pd.to_datetime(right[column], utc=True)
    try:
        pd.testing.assert_frame_equal(left, right, check_dtype=False)
    except AssertionError:
        return True
    return False


def write_daily(source: str, df: pd.DataFrame, *, directory: Path | None = None) -> tuple[int, int]:
    """Write a fetched frame date by date. Returns (new files, revisions).

    A date with no stored file gets its base file. A date whose newest stored
    version differs from the fetch gets the next ``-vN`` — never an overwrite.
    A date that matches what is stored gets nothing, which is the common case
    and what keeps the nightly re-fetch from producing daily ``-vN`` noise.

    ``directory`` exists for tests; production callers use the ``PATHS`` entry.
    """
    if df.empty:
        return 0, 0
    directory = directory or PATHS[source]
    directory.mkdir(parents=True, exist_ok=True)
    key = KEYS[source]

    new = revised = 0
    for day, group in df.groupby(pd.to_datetime(df["date"]).dt.date):
        group = group.reset_index(drop=True)
        newest = _latest_version(directory, day)
        if newest is None:
            group.to_parquet(directory / f"{day.isoformat()}.parquet", index=False)
            new += 1
            continue
        if not _differs(pd.read_parquet(newest), group, key):
            continue
        version = 2
        while (target := directory / f"{day.isoformat()}-v{version}.parquet").exists():
            version += 1
        group.to_parquet(target, index=False)
        revised += 1
    return new, revised


# --- the collectors each run invokes --------------------------------------


def kr_end(at: pd.Timestamp) -> dt.date:
    """The newest end date safe to fetch KR data for.

    Thin wrapper over :func:`src.util.session.last_closed_session`, which the
    renderer uses for the same question. Sharing it is the point: the two were
    written separately, only the collector got the guard, and on 2026-08-06 the
    renderer published thirty-one empty ratings because of the difference.
    """
    from src.util.session import last_closed_session

    return last_closed_session("KR", at)


def us_end(at: pd.Timestamp) -> dt.date:
    """The newest end date safe to fetch US data for.

    At the scheduled morning run (22:07 UTC) the US session of that UTC date
    has been closed for two hours, so today passes. The case this guards was
    hit on the first local run: mid-UTC-day, today's US session had not even
    opened, and the validator correctly reported all 48 tickers missing a
    trading day that had not happened — 48 false failures headed for the
    report header.
    """
    from src.util.session import last_closed_session

    return last_closed_session("US", at)


def collect_kr_price(start: dt.date, end: dt.date) -> tuple[str, ValidationReport]:
    end = min(end, kr_end(now_utc()))
    tickers = [e.ticker for e in load_watchlist(market="KR")]
    df, report = kr_price.fetch(tickers, start, end)
    new, revised = write_daily("kr_price", df)
    return f"{len(df)} rows, {new} new / {revised} revised", report


def collect_kr_flow(start: dt.date, end: dt.date) -> tuple[str, ValidationReport]:
    end = min(end, kr_end(now_utc()))
    tickers = [e.ticker for e in load_watchlist(market="KR")]
    df, report = kr_flow.fetch(tickers, start, end)
    new, revised = write_daily("kr_flow", df)
    return f"{len(df)} rows, {new} new / {revised} revised", report


def collect_kr_index(start: dt.date, end: dt.date) -> tuple[str, ValidationReport]:
    """The KODEX 200 benchmark. PREREGISTRATION §8.5's 3-month gate reads it.

    Evening only, alongside the other KRX sources. One ticker is one request,
    which is nothing against the ~250-request block `kr_flow`'s 124 sit under,
    so this adds no scheduling constraint.

    Nothing downstream of the pipeline consumes it — it is not a feature, not a
    rating input, and not in the watchlist. It is collected daily anyway because
    a benchmark fetched only at gate-reading time would be a benchmark nobody
    had ever validated.
    """
    end = min(end, kr_end(now_utc()))
    df, report = kr_index.fetch(start, end)
    new, revised = write_daily("kr_index", df)
    return f"{len(df)} rows, {new} new / {revised} revised", report


def collect_us_price(start: dt.date, end: dt.date) -> tuple[str, ValidationReport]:
    """Canonical US prices — Alpaca, evening only.

    ``end`` is clamped to yesterday UTC: Alpaca's free plan refuses any request
    whose ``end`` is at or after the current UTC day (HTTP 403, measured
    2026-08-06). By the evening run the UTC day has rolled past the session
    being fetched, so the clamp costs nothing.
    """
    end = min(end, now_utc().date() - dt.timedelta(days=1))
    symbols = [e.ticker for e in load_watchlist(market="US")] + list(us_price.INDEX_ETFS)
    df, report = us_price_alpaca.fetch(symbols, start, end)
    new, revised = write_daily("us_price", df)
    return f"{len(df)} rows, {new} new / {revised} revised", report


def collect_us_preview(start: dt.date, end: dt.date) -> tuple[str, ValidationReport]:
    """Morning preview of the just-closed US session — Tiingo.

    Written to ``price_preview/``, which the feature pipeline never reads; the
    two vendors are kept in separate series so a vendor switch can never inject
    a spurious return (notes/step11-plan.md). The evening Alpaca run supplies
    the canonical row for the same session, and the renderer compares the two.
    """
    end = min(end, us_end(now_utc()))
    symbols = [e.ticker for e in load_watchlist(market="US")] + list(us_price.INDEX_ETFS)
    df, report = us_price.fetch(symbols, start, end)
    new, revised = write_daily("us_price_preview", df)
    return f"{len(df)} rows, {new} new / {revised} revised", report


def collect_macro(start: dt.date, end: dt.date) -> tuple[str, ValidationReport]:
    # The driver's window is too short for the FX series; see MACRO_WINDOW_DAYS.
    del start
    df, report = macro.fetch(end - dt.timedelta(days=MACRO_WINDOW_DAYS), end)
    new, revised = write_daily("macro", df)
    return f"{len(df)} rows, {new} new / {revised} revised", report


def collect_calendar(start: dt.date, end: dt.date) -> tuple[str, ValidationReport]:
    # The driver's window answers a different question than this collector
    # needs — see CALENDAR_LOOKBACK_DAYS/CALENDAR_LOOKAHEAD_DAYS above.
    del start
    df, report = calendar_events.fetch(
        end - dt.timedelta(days=CALENDAR_LOOKBACK_DAYS),
        end + dt.timedelta(days=CALENDAR_LOOKAHEAD_DAYS),
    )
    new, revised = write_daily("calendar", df)
    return f"{len(df)} rows, {new} new / {revised} revised", report


def collect_us_filings(start: dt.date, end: dt.date) -> tuple[str, ValidationReport]:
    """SEC EDGAR filings. No KRX dependency, so this runs in both runs like
    ``macro``/``calendar`` — the evening run is what actually reaches the
    published report's "filing the previous day" flag, and the morning run
    costs only a handful of cheap, ungated SEC calls.
    """
    tickers = [e.ticker for e in load_watchlist(market="US")]
    cik_by_ticker = load_filing_ids()["us"]
    df, report = us_filings.fetch(tickers, cik_by_ticker, start, end)
    new, revised = write_daily("us_filings", df)
    return f"{len(df)} rows, {new} new / {revised} revised", report


def collect_kr_filings(start: dt.date, end: dt.date) -> tuple[str, ValidationReport]:
    """DART filings. Uses DART's OpenAPI, not pykrx/KRX scraping, so it does
    not carry kr_flow's KRX rate-limit constraint and runs in both runs.
    """
    tickers = [e.ticker for e in load_watchlist(market="KR")]
    corp_code_by_ticker = load_filing_ids()["kr"]
    df, report = kr_filings.fetch(tickers, corp_code_by_ticker, start, end)
    new, revised = write_daily("kr_filings", df)
    return f"{len(df)} rows, {new} new / {revised} revised", report


def collect_news(start: dt.date, end: dt.date) -> tuple[str, ValidationReport]:
    del start, end  # news has no date range; the buffer is whatever it is now
    now = now_utc()
    df, report = kr_news.fetch(load_news_feeds(), root=RAW, now=now)
    # Writes even when the frame is empty, for the reason kr_news.main()'s
    # docstring gives: the file's existence is the only record that the run
    # happened, and last_run_at reads the run clock off its filename.
    path = kr_news.write_run(df, RAW, now)
    return f"{len(df)} articles -> {path.name}", report


def collect_news_scores(start: dt.date, end: dt.date) -> tuple[str, ValidationReport]:
    """LLM-scores whatever's resolved-and-unscored in the recent news archive.
    No KRX dependency, same as ``kr_news``/``calendar``/the filings sources —
    runs in both runs. Not a date-range collector: ``daily_scoring`` finds its
    own candidates from ``data/raw/kr/news/`` and archives to ``data/scores/``.
    """
    del start, end  # see docstring — daily_scoring works off its own window
    df, report = daily_scoring.score_new_articles(DATA)
    return f"{len(df)} newly scored", report


Collector = Callable[[dt.date, dt.date], tuple[str, ValidationReport]]

# What each run collects, in order. The morning list containing no KRX source is
# a constraint, not a coincidence — tests/test_collect_daily.py pins it.
RUNS: dict[str, dict[str, Collector]] = {
    "morning": {
        "kr_news": collect_news,
        "macro": collect_macro,
        "calendar": collect_calendar,
        "us_price_preview": collect_us_preview,
        "us_filings": collect_us_filings,
        "kr_filings": collect_kr_filings,
        "news_scores": collect_news_scores,
    },
    "evening": {
        "kr_news": collect_news,
        "macro": collect_macro,
        "calendar": collect_calendar,
        "kr_price": collect_kr_price,
        "kr_flow": collect_kr_flow,
        "kr_index": collect_kr_index,
        "us_price": collect_us_price,
        "us_filings": collect_us_filings,
        "kr_filings": collect_kr_filings,
        "news_scores": collect_news_scores,
    },
}


def write_status(run: str, at: pd.Timestamp, outcomes: dict[str, dict]) -> Path:
    """Persist the run's outcome where the renderer will find it.

    This file is the bridge that gets check failures and news gaps into the
    report header. Without it they exist only in the Actions log, which CLAUDE.md
    names as exactly the place a failure must not live alone.
    """
    STATUS.mkdir(parents=True, exist_ok=True)
    path = STATUS / f"{run}-{at.strftime('%Y-%m-%dT%H%M%SZ')}.json"
    payload = {"run": run, "at": at.isoformat(), "collectors": outcomes}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--run", choices=sorted(RUNS), required=True)
    parser.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    args = parser.parse_args(argv)

    at = now_utc()
    end = at.date()
    start = end - dt.timedelta(days=args.window_days)
    print(f"collect_daily --run {args.run}  window {start}..{end}\n")

    outcomes: dict[str, dict] = {}
    for name, collector in RUNS[args.run].items():
        began = time.time()
        try:
            detail, report = collector(start, end)
            outcomes[name] = {
                "ok": report.ok,
                "detail": detail,
                "summary": report.summary(),
                "failures": [{"name": r.name, "detail": r.detail} for r in report.failures],
            }
        except Exception as exc:  # noqa: BLE001 - one source must not stop the rest
            outcomes[name] = {
                "ok": False,
                "detail": f"{type(exc).__name__}: {exc}",
                "summary": f"{name}: crashed before validating",
                "failures": [{"name": "crash", "detail": f"{type(exc).__name__}: {exc}"}],
            }
        mark = "ok " if outcomes[name]["ok"] else "FAIL"
        print(f"  {mark} {name:<18} {outcomes[name]['detail']}  [{time.time() - began:.0f}s]")
        for failure in outcomes[name]["failures"]:
            print(f"       - {failure['name']}: {failure['detail'][:140]}")

    path = write_status(args.run, at, outcomes)
    failed = [name for name, o in outcomes.items() if not o["ok"]]
    print(f"\nstatus -> {path}")
    if failed:
        print(f"failed collectors (the report will say so): {', '.join(failed)}")
    # Zero even on collector failure: the report publishes partially, and the
    # failures travel to its header via the status file. Non-zero is reserved
    # for the driver itself breaking, which the workflow's backstop notices.
    return 0


if __name__ == "__main__":
    sys.exit(main())
