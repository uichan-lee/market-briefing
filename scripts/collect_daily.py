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

Both runs also collect ``kr_news``: the hourly workflow already covers the
clock, but an extra poll costs nothing (dedup drops what is already stored) and
it is what produces a *fresh* feed-continuity check for the report header.

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

from src.collectors import kr_flow, kr_news, kr_price, macro, us_price, us_price_alpaca
from src.collectors.validate import ValidationReport
from src.util.config import load_news_feeds, load_watchlist
from src.util.session import now_utc

ROOT = Path(__file__).resolve().parents[1]
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

PATHS = {
    "kr_price": RAW / "kr" / "price",
    "kr_flow": RAW / "kr" / "investor_flow",
    "us_price": RAW / "us" / "price",
    "us_price_preview": RAW / "us" / "price_preview",
    "macro": RAW / "macro",
}

# Row identity per source, for the changed-content comparison.
KEYS = {
    "kr_price": ["date", "ticker"],
    "kr_flow": ["date", "ticker"],
    "us_price": ["date", "ticker"],
    "us_price_preview": ["date", "ticker"],
    "macro": ["date", "series"],
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
    columns = sorted(stored.columns)
    left = stored.sort_values(key)[columns].reset_index(drop=True)
    right = fetched.sort_values(key)[columns].reset_index(drop=True)
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


def collect_news(start: dt.date, end: dt.date) -> tuple[str, ValidationReport]:
    del start, end  # news has no date range; the buffer is whatever it is now
    now = now_utc()
    df, report = kr_news.fetch(load_news_feeds(), root=RAW, now=now)
    # Writes even when the frame is empty, for the reason kr_news.main()'s
    # docstring gives: the file's existence is the only record that the run
    # happened, and last_run_at reads the run clock off its filename.
    path = kr_news.write_run(df, RAW, now)
    return f"{len(df)} articles -> {path.name}", report


Collector = Callable[[dt.date, dt.date], tuple[str, ValidationReport]]

# What each run collects, in order. The morning list containing no KRX source is
# a constraint, not a coincidence — tests/test_collect_daily.py pins it.
RUNS: dict[str, dict[str, Collector]] = {
    "morning": {
        "kr_news": collect_news,
        "macro": collect_macro,
        "us_price_preview": collect_us_preview,
    },
    "evening": {
        "kr_news": collect_news,
        "macro": collect_macro,
        "kr_price": collect_kr_price,
        "kr_flow": collect_kr_flow,
        "us_price": collect_us_price,
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
