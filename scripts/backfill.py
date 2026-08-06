"""Three-year historical backfill into ``data/raw/``. SPEC §12 step 4.

    uv run python -m scripts.backfill --years 3
    uv run python -m scripts.backfill --sources us_price macro
    uv run python -m scripts.backfill --sources kr_price kr_flow   # resume

Writes the layout SPEC §3.3 specifies — one parquet per source per session date
— so a backfilled day and a day the daily pipeline collects are indistinguishable
downstream.

Designed to be re-run
---------------------
The KR sources scrape KRX, and KRX throttles by address: a 31-ticker run costs
124 requests, and a few of those in an evening is enough to earn an HTML error
page instead of JSON for a while. A backfill that had to complete in one pass
would lose everything each time that happened.

So every date already written is skipped, and interrupting the script costs only
the range in flight. Running it again after a block simply continues. That is
also why the sources are independent: US and macro do not touch KRX and complete
regardless of what KRX is doing.

Never overwrites
----------------
CLAUDE.md rule 1: nothing under ``data/raw/`` is overwritten. A date that
already has a file is skipped entirely by default. ``--revise`` re-fetches and
writes beside the original with a ``-v2`` suffix, which is the same convention
the collectors use for a re-run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import pandas as pd

from src.collectors import kr_flow, kr_price, macro, us_price, us_price_alpaca
from src.util.config import load_watchlist
from src.util.krx import KrxSessionError
from src.util.session import trading_days

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"

# SPEC §3.3. The directory is the unit a downstream loader globs over, so these
# names are part of the contract rather than a convenience.
PATHS = {
    "kr_price": RAW / "kr" / "price",
    "kr_flow": RAW / "kr" / "investor_flow",
    "us_price": RAW / "us" / "price",
    "macro": RAW / "macro",
}

# KRX is fetched a year at a time. One request per ticker per endpoint for a
# whole year keeps the request count at its floor, while still bounding how much
# is lost when a block lands mid-run.
_KR_CHUNK_DAYS = 365


def _target(source: str, day: dt.date, *, revise: bool) -> Path | None:
    """Where ``day`` goes, or ``None`` if it is already there and should stay."""
    directory = PATHS[source]
    base = directory / f"{day.isoformat()}.parquet"
    if not base.exists():
        return base
    if not revise:
        return None
    # CLAUDE.md rule 1: the original stays. Suffix until a free name appears.
    version = 2
    while (candidate := directory / f"{day.isoformat()}-v{version}.parquet").exists():
        version += 1
    return candidate


def _write_by_date(source: str, df: pd.DataFrame, *, revise: bool) -> int:
    """Split a multi-day frame into one parquet per session date."""
    if df.empty:
        return 0
    PATHS[source].mkdir(parents=True, exist_ok=True)

    written = 0
    for day, group in df.groupby(pd.to_datetime(df["date"]).dt.date):
        path = _target(source, day, revise=revise)
        if path is None:
            continue
        group.reset_index(drop=True).to_parquet(path, index=False)
        written += 1
    return written


def _pending(source: str, start: dt.date, end: dt.date, market: str) -> list[dt.date]:
    """Session dates in range with nothing written yet."""
    return [
        day
        for day in trading_days(market, start, end)
        if not (PATHS[source] / f"{day.isoformat()}.parquet").exists()
    ]


def _chunks(start: dt.date, end: dt.date, days: int):
    cursor = start
    while cursor <= end:
        stop = min(cursor + dt.timedelta(days=days - 1), end)
        yield cursor, stop
        cursor = stop + dt.timedelta(days=1)


# --- per-source backfills -------------------------------------------------


def backfill_macro(start: dt.date, end: dt.date, *, revise: bool) -> str:
    df, report = macro.fetch(start, end)
    written = _write_by_date("macro", df, revise=revise)
    return f"macro     {len(df):>6} rows -> {written:>4} files | {report.summary()}"


def backfill_us_price(start: dt.date, end: dt.date, *, revise: bool) -> str:
    symbols = [e.ticker for e in load_watchlist(market="US")] + list(us_price.INDEX_ETFS)
    df, report = us_price_alpaca.fetch(symbols, start, end)
    written = _write_by_date("us_price", df, revise=revise)
    return f"us_price  {len(df):>6} rows -> {written:>4} files | {report.summary()}"


def _backfill_kr(source: str, fetch, start: dt.date, end: dt.date, *, revise: bool) -> str:
    tickers = [e.ticker for e in load_watchlist(market="KR")]
    pending = _pending(source, start, end, "KR")
    if not pending and not revise:
        return f"{source:<9} nothing pending; every session already written"

    total_rows = written = 0
    for chunk_start, chunk_end in _chunks(min(pending), max(pending), _KR_CHUNK_DAYS):
        try:
            df, report = fetch(tickers, chunk_start, chunk_end)
        except KrxSessionError as exc:
            return (
                f"{source:<9} STOPPED at {chunk_start}: {exc} "
                f"[{written} files written; re-run to continue]"
            )

        # The collectors *report* a refused KRX session rather than raising it,
        # which is what CLAUDE.md wants of a collector but not what a backfill
        # driver can act on. Without this the loop walked every chunk, wrote
        # nothing, and returned "0 rows -> 0 files" as though that were a
        # normal outcome — a silent failure produced by the very script meant
        # to fill the dataset. Observed on 2026-08-05: forty scheduled attempts
        # over three hours, all no-ops, none of them saying why.
        refused = next((c for c in report.failures if c.name == "krx_session"), None)
        if refused:
            return (
                f"{source:<9} STOPPED at {chunk_start}: {refused.detail} "
                f"[{written} files written; re-run to continue]"
            )
        if not report.ok:
            named = ", ".join(c.name for c in report.failures[:3])
            print(f"    {chunk_start}..{chunk_end}: {len(report.failures)} failed — {named}")
        total_rows += len(df)
        written += _write_by_date(source, df, revise=revise)
        print(f"    {chunk_start}..{chunk_end}: {len(df)} rows, {written} files so far", flush=True)

    return f"{source:<9} {total_rows:>6} rows -> {written:>4} files"


def backfill_kr_price(start: dt.date, end: dt.date, *, revise: bool) -> str:
    return _backfill_kr("kr_price", kr_price.fetch, start, end, revise=revise)


def backfill_kr_flow(start: dt.date, end: dt.date, *, revise: bool) -> str:
    return _backfill_kr("kr_flow", kr_flow.fetch, start, end, revise=revise)


SOURCES = {
    "macro": backfill_macro,
    "us_price": backfill_us_price,
    "kr_price": backfill_kr_price,
    "kr_flow": backfill_kr_flow,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--years", type=float, default=3.0)
    parser.add_argument("--end", type=dt.date.fromisoformat, default=None)
    parser.add_argument("--sources", nargs="+", choices=sorted(SOURCES), default=sorted(SOURCES))
    parser.add_argument(
        "--revise",
        action="store_true",
        help="re-fetch dates already written, saving beside them with a -v2 suffix",
    )
    args = parser.parse_args(argv)

    end = args.end or dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=round(args.years * 365.25))

    print(f"backfill {start} .. {end}  ({args.years:g}y)")
    print(f"sources: {', '.join(args.sources)}\n")

    results = []
    for name in args.sources:
        print(f"--- {name} ---", flush=True)
        began = time.time()
        try:
            line = SOURCES[name](start, end, revise=args.revise)
        except Exception as exc:  # noqa: BLE001 - one source must not stop the rest
            line = f"{name:<9} ERROR {type(exc).__name__}: {exc}"
        results.append(f"{line}  [{time.time() - began:.0f}s]")
        print(results[-1] + "\n", flush=True)

    print("=" * 72)
    for line in results:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
